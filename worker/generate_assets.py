#!/usr/bin/env python3
"""
generate_assets.py - builds every scene's voice track and face-locked image.
Runs on the GitHub Actions runner (NOT on Render), so n8n stays light.

Inputs (inside --workdir, downloaded from the video's Drive folder by the workflow):
  characters.json   rows of the "Characters" sheet
  storyboard.json   rows of the "Storyboard" sheet
Outputs (inside --workdir):
  avatars/<Name_With_Underscores>.png   reference faces (only for scenes that still need an image)
  scene_<N>.mp3                         narration/dialogue, one voice per speaker
  scene_<N>.png                         InstantID image locked to the scene's main character

Backends (env IMAGE_BACKEND, or --images):
  instantid    real face-locked images from the Hugging Face Space (default)
  placeholder  black test frames, no GPU quota used (marked inside the PNG so they are never mistaken for real images)

Motion (env VIDEO_BACKEND, or --video; default none):
  none         no motion clips (scenes render as slow-zoom stills)
  wan          image-to-video through a Hugging Face Wan 2.x Space -> scene_<N>.mp4 (3-5 s moving clip)
  placeholder  fake slow-zoom clips, no GPU, to test the timing/render logic
Optional storyboard columns: motion_prompt_en (what should move), animate (no/false/0 = keep this scene a still)
Env for wan (optional): WAN_SPACE (default zerogpu-aoti/wan2-2-fp8da-aoti-faster), WAN_CLIP_SECONDS (3), WAN_STEPS (4),
                        REQUIRE_VIDEO=1 (fail with exit 3 instead of rendering stills when a clip cannot be made)

Re-running resumes: finished voices/images are skipped. A placeholder image is NOT treated as finished when the
backend is instantid: it is replaced automatically.

Env (optional): HF_TOKEN, INSTANTID_SPACE (default InstantX/InstantID),
                INSTANTID_LCM (default 1 = fast LCM mode, 5 steps; set 0 for 20-step quality mode)

Exit codes: 0 = all done, 2 = some scenes failed, 3 = GPU quota exhausted (nothing more is attempted).
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

NARRATOR_VOICE = "en-US-ChristopherNeural"
RADIO_DJ_VOICE = "en-US-EricNeural"
FALLBACK_VOICE = "en-US-GuyNeural"
PAUSE_SECONDS = 0.3
NEGATIVE_PROMPT = "blurry, low quality, distorted face, extra limbs, watermark, deformed, text, logo"
QUOTA_HINTS = ("quota", "zerogpu", "exceeded your", "too many requests", "429", "rate limit")
PLACEHOLDER_KEY = b"AIDRAMA_PLACEHOLDER"
DEFAULT_WAN_SPACE = "zerogpu-aoti/wan2-2-fp8da-aoti-faster"
WAN_NEGATIVE_PROMPT = ("blurry, low quality, static, still image, frozen, distorted face, deformed, extra limbs, "
                       "morphing, flicker, watermark, text, subtitles")
PLACEHOLDER_MAX_BYTES = 40_000   # a black 1080x1920 PNG is ~6 KB; a real generated image is far larger


class QuotaError(RuntimeError):
    """Raised when the Hugging Face Space refuses work because the GPU quota is used up."""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- helpers ---
def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "", re.sub(r"\s+", "_", name.strip()))


def scene_num(scene):
    return int(float(str(scene["scene_number"]).strip()))


def extract_drive_id(url):
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url or "") or re.search(r"/d/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def parse_scene_filter(text):
    """'1-3,7' -> {1,2,3,7}; empty -> None (meaning: all scenes)."""
    text = (text or "").strip()
    if not text:
        return None
    wanted = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(range(int(a), int(b) + 1))
        else:
            wanted.add(int(part))
    return wanted


def non_empty(path):
    return path.exists() and path.stat().st_size > 0


# --------------------------------------------------------- placeholders -----
def mark_png_as_placeholder(path):
    """Insert a tiny tEXt chunk right after the IHDR chunk (survives Drive round trips)."""
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("placeholder is not a PNG")
    payload = PLACEHOLDER_KEY + b"\x00" + b"1"
    chunk = struct.pack(">I", len(payload)) + b"tEXt" + payload + struct.pack(">I", zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF)
    ihdr_end = 8 + 4 + 4 + 13 + 4      # signature + length + type + data + crc
    path.write_bytes(data[:ihdr_end] + chunk + data[ihdr_end:])


def is_placeholder(path):
    if not non_empty(path):
        return False
    with open(path, "rb") as f:
        head = f.read(256)
    # marked placeholders, or old unmarked black test frames (they are tiny compared with real images)
    return PLACEHOLDER_KEY in head or path.stat().st_size < PLACEHOLDER_MAX_BYTES


def make_placeholder_image(out_path):
    """Black portrait frame for pipeline tests without GPU quota."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920",
                    "-frames:v", "1", str(out_path)], check=True)
    if not non_empty(out_path):
        raise RuntimeError("placeholder image was not created")
    mark_png_as_placeholder(out_path)


def image_needed(png, backend):
    """True if scene_N.png still has to be generated."""
    if not non_empty(png):
        return True
    return backend == "instantid" and is_placeholder(png)


# ------------------------------------------------------------ text -> voice --
def name_aliases(name):
    tokens = name.split()
    aliases = {name.strip()}
    if tokens:
        # "Philip Sterling" -> "Philip";  "Mr. Reyes" -> "Reyes"
        aliases.add(tokens[-1] if tokens[0].endswith(".") else tokens[0])
    return {a for a in aliases if a}


def build_voice_map(characters):
    voices = {"narration": NARRATOR_VOICE, "narrator": NARRATOR_VOICE, "radio dj": RADIO_DJ_VOICE}
    for c in characters:
        voice = (c.get("voice_edge_tts") or "").strip() or FALLBACK_VOICE
        for alias in name_aliases(c["character_name"]):
            voices[alias.lower()] = voice
    return voices


def build_speaker_regex(voice_map):
    labels = sorted(voice_map.keys(), key=len, reverse=True)
    alts = "|".join(re.escape(label) for label in labels)
    return re.compile(rf"(?:^|(?<=\s))({alts})\s*:\s*", re.IGNORECASE)


def clean_speech(text):
    text = text.replace("\\!", "!")
    text = re.sub(r"\([^)]*\)", " ", text)                      # stage directions
    text = re.sub(r"[\"\u201c\u201d]", "", text)                # quote marks
    text = re.sub(r"\$\s?(\d[\d,.]*)\s*(billion|million|thousand)\b", r"\1 \2 dollars", text, flags=re.I)
    text = re.sub(r"\$\s?(\d[\d,.]*)", r"\1 dollars", text)
    return re.sub(r"\s+", " ", text).strip()


def split_segments(text, rx):
    """'Narration: A  Philip: (x) "B"' -> [('Narration','A'), ('Philip','B')]"""
    text = str(text or "").strip()
    matches = list(rx.finditer(text))
    if not matches:
        return [s for s in [("Narration", clean_speech(text))] if s[1]]
    segments = []
    lead = text[: matches[0].start()].strip()
    if lead:
        segments.append(("Narration", clean_speech(lead)))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segments.append((m.group(1), clean_speech(text[m.end():end])))
    return [(label, t) for label, t in segments if t]


async def _synth(text, voice, out_path):
    import edge_tts  # imported lazily so the pure functions can be tested without it
    await edge_tts.Communicate(text, voice).save(str(out_path))


def synth_with_retry(text, voice, out_path, attempts=3):
    for i in range(1, attempts + 1):
        try:
            asyncio.run(_synth(text, voice, out_path))
            if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
                return
            raise RuntimeError("edge-tts produced an empty file")
        except Exception as exc:  # noqa: BLE001
            if i == attempts:
                raise
            log(f"    TTS retry {i}/{attempts - 1} after: {exc}")
            time.sleep(5 * i)


def make_scene_audio(scene, rx, voice_map, workdir):
    num = scene_num(scene)
    out = workdir / f"scene_{num}.mp3"
    if non_empty(out):
        return "skipped"
    segments = split_segments(scene["dialogue_or_narration"], rx)
    if not segments:
        raise RuntimeError(f"scene {num}: no speakable text")
    tmp = workdir / f"_tts_{num}"
    tmp.mkdir(exist_ok=True)
    parts = []
    for i, (label, text) in enumerate(segments):
        voice = voice_map.get(label.lower(), FALLBACK_VOICE)
        part = tmp / f"seg_{i}.mp3"
        synth_with_retry(text, voice, part)
        parts.append(part)
    if len(parts) == 1:
        shutil.move(str(parts[0]), out)
    else:
        silence = workdir / "_silence.mp3"
        if not non_empty(silence):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                            "-t", str(PAUSE_SECONDS), "-c:a", "libmp3lame", "-b:a", "48k", str(silence)], check=True)
        lines = []
        for i, part in enumerate(parts):
            if i:
                lines.append(f"file '{silence.resolve()}'")
            lines.append(f"file '{part.resolve()}'")
        (tmp / "list.txt").write_text("\n".join(lines) + "\n")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                        "-c:a", "libmp3lame", "-b:a", "96k", str(out)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return f"{len(segments)} segment(s)"


# ------------------------------------------------------------ avatars -------
def looks_like_image(path):
    head = path.read_bytes()[:12] if path.exists() else b""
    return head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8") or head[:4] == b"RIFF"


def fetch_avatar(character, dest_dir):
    name = character["character_name"].strip()
    dest = dest_dir / f"{safe_name(name)}.png"
    if non_empty(dest) and looks_like_image(dest):
        return dest
    dest.unlink(missing_ok=True)
    url = character.get("avatar_reference_url", "")
    file_id = extract_drive_id(url)
    if not file_id:
        raise RuntimeError(f"{name}: avatar_reference_url has no Drive file id")
    # 1) authenticated copy through rclone (works for private files in the same Google account)
    res = subprocess.run(["rclone", "backend", "copyid", "gdrive:", file_id, str(dest)],
                         capture_output=True, text=True, timeout=180)
    if res.returncode == 0 and non_empty(dest) and looks_like_image(dest):
        return dest
    log(f"  rclone could not fetch {name}'s avatar ({res.stderr.strip()[:160]}); trying the public link")
    # 2) public 'anyone with the link' download
    import httpx
    r = httpx.get(url, follow_redirects=True, timeout=90)
    dest.write_bytes(r.content)
    if not looks_like_image(dest):
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: avatar download did not return an image (is the Drive file shared with this account?)")
    return dest


# ------------------------------------------------------------ InstantID -----
def pick_primary(scene, characters_by_name):
    """Face to lock = the listed character that the image prompt mentions first."""
    names = [n.strip() for n in str(scene.get("characters_in_scene", "")).split(",") if n.strip()]
    if not names:
        raise RuntimeError(f"scene {scene_num(scene)}: characters_in_scene is empty")
    prompt = str(scene.get("visual_prompt_en", ""))
    found = sorted((prompt.find(n), n) for n in names if n in prompt)
    chosen = found[0][1] if found else names[0]
    if chosen not in characters_by_name:
        raise RuntimeError(f"scene {scene_num(scene)}: unknown character '{chosen}'")
    return chosen


def lcm_enabled():
    return os.environ.get("INSTANTID_LCM", "1").strip().lower() not in ("0", "false", "no", "off")


def build_kwargs(params, face_path, prompt, seed, handle_file, lcm=None):
    """
    Only pass parameters the Space currently exposes, matching names case-insensitively
    (the Space's parameter is 'enable_LCM' while its usage text shows 'enable_lcm').
    LCM mode mirrors what the Space's own toggle sets: 5 steps, guidance 1.5.
    """
    lcm = lcm_enabled() if lcm is None else lcm
    known = {
        "face_image_path": lambda: handle_file(str(face_path)),
        "pose_image_path": lambda: None,
        "prompt": lambda: prompt,
        "negative_prompt": lambda: NEGATIVE_PROMPT,
        "style_name": lambda: "(No style)",
        "num_steps": lambda: 5 if lcm else 20,
        "identitynet_strength_ratio": lambda: 0.85,
        "adapter_strength_ratio": lambda: 0.75,
        "pose_strength": lambda: 0.3,
        "canny_strength": lambda: 0.3,
        "depth_strength": lambda: 0.3,
        "controlnet_selection": lambda: [],      # identity-only: canny/depth need a pose image
        "guidance_scale": lambda: 1.5 if lcm else 5.0,
        "seed": lambda: seed,
        "scheduler": lambda: "EulerDiscreteScheduler",
        "enable_lcm": lambda: lcm,
        "enhance_face_region": lambda: True,
    }
    kwargs, missing = {}, []
    for p in params:
        pname = p["parameter_name"]
        key = pname.lower()
        if key in known:
            kwargs[pname] = known[key]()
        elif not p.get("parameter_has_default"):
            missing.append(pname)
    if missing:
        raise RuntimeError("The Space API changed; unknown required parameters: " + ", ".join(missing))
    lowered = {k.lower() for k in kwargs}
    if "face_image_path" not in lowered or "prompt" not in lowered:
        raise RuntimeError("The Space API changed; no face_image_path/prompt parameter found")
    return kwargs


def extract_result_path(result):
    r = result
    while isinstance(r, (list, tuple)) and r:
        r = r[0]
    if isinstance(r, dict):
        r = r.get("video") or r.get("path") or r.get("value") or r.get("name")
        if isinstance(r, dict):
            r = r.get("path")
    if not isinstance(r, str) or not Path(r).exists():
        raise RuntimeError(f"unexpected result from the Space: {str(result)[:200]}")
    return r


class InstantIDClient:
    def __init__(self):
        self.client = None
        self.endpoint = None
        self.params = None
        self.handle_file = None

    def connect(self):
        from gradio_client import Client, handle_file
        self.handle_file = handle_file
        space = os.environ.get("INSTANTID_SPACE", "InstantX/InstantID")
        token = (os.environ.get("HF_TOKEN") or "").strip() or None
        log(f"Connecting to Hugging Face Space {space} ({'with' if token else 'without'} token, "
            f"{'LCM fast' if lcm_enabled() else 'standard 20-step'} mode)")
        if token:
            try:
                self.client = Client(space, token=token)
            except TypeError:  # older gradio_client versions call it hf_token
                self.client = Client(space, hf_token=token)
        else:
            self.client = Client(space)
        api = self.client.view_api(return_format="dict")
        endpoints = api.get("named_endpoints", {})
        name = "/generate_image" if "/generate_image" in endpoints else next((k for k in endpoints if "generate" in k), None)
        if not name:
            raise RuntimeError("The Space has no generate endpoint; available: " + ", ".join(endpoints))
        self.endpoint, self.params = name, endpoints[name]["parameters"]
        log(f"Using endpoint {name} with parameters: {', '.join(p['parameter_name'] for p in self.params)}")

    def generate(self, face_path, prompt, seed, out_path):
        if self.client is None:
            self.connect()
        kwargs = build_kwargs(self.params, face_path, prompt, seed, self.handle_file)
        delays = [20, 60, 120]
        for attempt in range(len(delays) + 1):
            try:
                result = self.client.predict(api_name=self.endpoint, **kwargs)
                shutil.copy(extract_result_path(result), out_path)
                return
            except Exception as exc:  # noqa: BLE001
                text = str(exc).lower()
                if any(h in text for h in QUOTA_HINTS):
                    raise QuotaError(str(exc)) from exc
                if attempt == len(delays):
                    raise
                log(f"    image retry {attempt + 1}/{len(delays)} in {delays[attempt]}s after: {str(exc)[:200]}")
                time.sleep(delays[attempt])


# ------------------------------------------------------- motion (Wan) ------
def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def wants_video(scene):
    """Optional storyboard column 'animate': no/false/0/off keeps the scene a still."""
    return str(scene.get("animate", "")).strip().lower() not in ("no", "n", "false", "0", "off", "skip", "still")


def motion_prompt(scene):
    """What should MOVE (the image already defines what is in the shot)."""
    custom = str(scene.get("motion_prompt_en", "") or "").strip()
    if custom:
        return re.sub(r"\s+", " ", custom)
    mood = str(scene.get("mood", "") or "").strip().lower()
    mood_part = f" {mood} mood." if mood else ""
    return (f"Cinematic drama shot.{mood_part} The characters move naturally: subtle breathing, small head and body "
            "movements, natural facial expressions, blinking. Slow smooth camera push-in. Realistic motion, stable identity.")


def mp4_is_placeholder(path):
    if not non_empty(path) or path.stat().st_size > 30_000_000:
        return False
    return PLACEHOLDER_KEY in path.read_bytes()


def video_needed(mp4, backend):
    if not non_empty(mp4):
        return True
    return backend == "wan" and mp4_is_placeholder(mp4)


def prepare_wan_input(png, out_path):
    """Center-crop to 9:16 (same framing render.sh will use) and shrink, so Wan gets a portrait image."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(png), "-vf",
                    "crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)',scale=480:854", "-frames:v", "1", str(out_path)], check=True)


def make_placeholder_clip(png, out_path, seconds=3.0):
    """Fake 'motion' clip (slow zoom, 16 fps) marked as placeholder; for testing the render/timing logic."""
    frames = max(8, int(seconds * 16))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png), "-t", str(seconds), "-vf",
                    f"scale=480:854:force_original_aspect_ratio=increase,crop=480:854,"
                    f"zoompan=z='1+0.1*on/{frames}':d={frames}:s=480x854:fps=16,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-metadata", f"comment={PLACEHOLDER_KEY.decode()}",
                    "-movflags", "+faststart", str(out_path)], check=True)
    if not non_empty(out_path):
        raise RuntimeError("placeholder clip was not created")


IMAGE_PARAM_NAMES = {"input_image", "image", "start_image", "first_image", "image_path", "input_img", "init_image"}
LAST_IMAGE_HINTS = ("last", "end", "final")


def is_image_param(p):
    """Image input? Uses the Space's own type info when present (component 'Image' / filepath), else the name."""
    key = p["parameter_name"].lower()
    component = str(p.get("component", "")).lower()
    python_type = str((p.get("python_type") or {}).get("type", "")).lower()
    if component in ("image", "imageeditor") or key in IMAGE_PARAM_NAMES:
        return True
    return python_type == "filepath" and ("image" in key or "img" in key)


def build_wan_kwargs(params, image_path, prompt, seed, seconds, steps, handle_file):
    """Map our values onto the Space's CURRENT parameters (case-insensitive); skip optional unknowns; fail on unknown required ones."""
    known = {
        "prompt": lambda: prompt,
        "negative_prompt": lambda: WAN_NEGATIVE_PROMPT,
        "steps": lambda: steps, "num_inference_steps": lambda: steps, "sampling_steps": lambda: steps,
        "duration_seconds": lambda: seconds, "duration": lambda: seconds,
        "guidance_scale": lambda: 1.0, "guidance_scale_2": lambda: 1.0,
        "seed": lambda: seed, "randomize_seed": lambda: False,
    }
    kwargs, missing, image_set = {}, [], False
    for p in params:
        pname = p["parameter_name"]
        key = pname.lower()
        if is_image_param(p):
            if any(h in key for h in LAST_IMAGE_HINTS):
                if not p.get("parameter_has_default"):
                    kwargs[pname] = None            # optional last-frame image: not used
            elif not image_set:
                kwargs[pname] = handle_file(str(image_path))
                image_set = True
            elif not p.get("parameter_has_default"):
                kwargs[pname] = None
        elif key in known:
            kwargs[pname] = known[key]()
        elif not p.get("parameter_has_default"):
            missing.append(pname)
    if missing:
        raise RuntimeError("The Wan Space API changed; unknown required parameters: " + ", ".join(missing))
    if not image_set:
        raise RuntimeError("The Wan Space API changed; no input image parameter found")
    return kwargs


class WanClient:
    def __init__(self):
        self.client = None
        self.endpoint = None
        self.params = None
        self.handle_file = None

    def connect(self):
        from gradio_client import Client, handle_file
        self.handle_file = handle_file
        space = os.environ.get("WAN_SPACE", DEFAULT_WAN_SPACE)
        token = (os.environ.get("HF_TOKEN") or "").strip() or None
        log(f"Connecting to Wan Space {space} ({'with' if token else 'without'} token)")
        if token:
            try:
                self.client = Client(space, token=token)
            except TypeError:
                self.client = Client(space, hf_token=token)
        else:
            self.client = Client(space)
        api = self.client.view_api(return_format="dict")
        endpoints = api.get("named_endpoints", {})
        name = next((k for k in endpoints if "generate" in k and "video" in k), None) \
            or next((k for k in endpoints if "generate" in k), None) \
            or next((k for k in endpoints if "video" in k), None)
        if not name:
            raise RuntimeError("The Wan Space has no generate/video endpoint; available: " + ", ".join(endpoints))
        self.endpoint, self.params = name, endpoints[name]["parameters"]
        log(f"Using Wan endpoint {name} with parameters: {', '.join(p['parameter_name'] for p in self.params)}")

    def generate(self, image_path, prompt, seed, out_path, seconds, steps):
        if self.client is None:
            self.connect()
        kwargs = build_wan_kwargs(self.params, image_path, prompt, seed, seconds, steps, self.handle_file)
        delays = [20, 60, 120]
        for attempt in range(len(delays) + 1):
            try:
                result = self.client.predict(api_name=self.endpoint, **kwargs)
                shutil.copy(extract_result_path(result), out_path)
                return
            except Exception as exc:  # noqa: BLE001
                text = str(exc).lower()
                if any(h in text for h in QUOTA_HINTS):
                    raise QuotaError(str(exc)) from exc
                if attempt == len(delays):
                    raise
                log(f"    clip retry {attempt + 1}/{len(delays)} in {delays[attempt]}s after: {str(exc)[:200]}")
                time.sleep(delays[attempt])


def make_scene_video(scene, png, out_mp4, backend, wan, seconds, steps, by_name, work):
    """Returns 'wan' or 'placeholder'. Raises QuotaError / RuntimeError."""
    if backend == "placeholder":
        make_placeholder_clip(png, out_mp4, seconds)
        return "placeholder"
    num = scene_num(scene)
    wan_in = work / f"_wan_in_{num}.png"
    prepare_wan_input(png, wan_in)
    try:
        primary = pick_primary(scene, by_name)
        seed = int(float(by_name[primary].get("avatar_seed") or 42))
    except Exception:  # noqa: BLE001
        seed = 42
    wan.generate(wan_in, motion_prompt(scene), seed, out_mp4, seconds, steps)
    wan_in.unlink(missing_ok=True)
    return "wan"


# ------------------------------------------------------------------ main ----
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--scenes", default="", help="optional filter, e.g. '1-3,7' (default: all scenes)")
    ap.add_argument("--images", choices=["instantid", "placeholder"], default=None,
                    help="image backend; default comes from env IMAGE_BACKEND, else instantid")
    ap.add_argument("--video", choices=["none", "wan", "placeholder"], default=None,
                    help="motion backend; default comes from env VIDEO_BACKEND, else none")
    args = ap.parse_args(argv)

    backend = (args.images or os.environ.get("IMAGE_BACKEND") or "instantid").strip().lower()
    if backend not in ("instantid", "placeholder"):
        raise RuntimeError(f"Unknown IMAGE_BACKEND '{backend}'. Use 'instantid' or 'placeholder'.")
    video_backend = (args.video or os.environ.get("VIDEO_BACKEND") or "none").strip().lower()
    if video_backend not in ("none", "wan", "placeholder"):
        raise RuntimeError(f"Unknown VIDEO_BACKEND '{video_backend}'. Use 'none', 'wan' or 'placeholder'.")
    require_video = os.environ.get("REQUIRE_VIDEO", "0").strip().lower() in ("1", "true", "yes")
    clip_seconds = env_float("WAN_CLIP_SECONDS", 3.0)
    wan_steps = int(env_float("WAN_STEPS", 4))
    log(f"Image backend: {backend} | Video backend: {video_backend}"
        + (f" ({clip_seconds:g}s clips, {wan_steps} steps)" if video_backend != "none" else ""))

    work = Path(args.workdir)
    characters = json.loads((work / "characters.json").read_text(encoding="utf-8"))
    scenes = json.loads((work / "storyboard.json").read_text(encoding="utf-8"))
    scenes = sorted(scenes, key=scene_num)
    wanted = parse_scene_filter(args.scenes)
    if wanted is not None:
        scenes = [s for s in scenes if scene_num(s) in wanted]
    if not scenes:
        log("No scenes selected.")
        return 2

    by_name = {c["character_name"].strip(): c for c in characters}
    voice_map = build_voice_map(characters)
    rx = build_speaker_regex(voice_map)

    # Stale placeholders must be replaced when real images / real clips are requested.
    avatars = {}
    if backend == "instantid":
        for s in scenes:
            png = work / f"scene_{scene_num(s)}.png"
            if non_empty(png) and is_placeholder(png):
                log(f"Scene {scene_num(s)}: existing image is a placeholder, it will be regenerated")
                png.unlink()
        pending = [s for s in scenes if image_needed(work / f"scene_{scene_num(s)}.png", backend)]
        avatars_dir = work / "avatars"
        avatars_dir.mkdir(exist_ok=True)
        for name in sorted({pick_primary(s, by_name) for s in pending}):
            avatars[name] = fetch_avatar(by_name[name], avatars_dir)
            log(f"Avatar ready: {name}")

    instantid = InstantIDClient()
    wan = WanClient()
    failed, video_missing = [], []
    wan_quota_hit = False
    total = len(scenes)
    for idx, scene in enumerate(scenes, 1):
        num = scene_num(scene)
        log(f"Scene {num} ({idx}/{total})")
        try:
            log(f"  voice: {make_scene_audio(scene, rx, voice_map, work)}")
        except Exception as exc:  # noqa: BLE001
            log(f"  VOICE FAILED: {exc}")
            failed.append((num, "voice", str(exc)))
            continue

        # ---- image ----
        out_png = work / f"scene_{num}.png"
        image_ok = True
        if image_needed(out_png, backend):
            try:
                if backend == "placeholder":
                    make_placeholder_image(out_png)
                    log("  image: placeholder generated (test mode, no GPU used)")
                else:
                    primary = pick_primary(scene, by_name)
                    seed = int(float(by_name[primary].get("avatar_seed") or 42))
                    prompt = re.sub(r"\s+", " ", str(scene["visual_prompt_en"])).strip()
                    log(f"  image: face lock = {primary}, seed = {seed}")
                    started = time.time()
                    instantid.generate(avatars[primary], prompt, seed, out_png)
                    log(f"  image: done in {time.time() - started:.0f}s (wall clock, includes queue time)")
            except QuotaError as exc:
                log(f"GPU QUOTA EXHAUSTED at scene {num} (image): {str(exc)[:300]}")
                log("Re-run after the quota resets (24 h after first GPU use), or upgrade the Hugging Face plan. Finished scenes are kept.")
                return 3
            except Exception as exc:  # noqa: BLE001
                log(f"  IMAGE FAILED: {exc}")
                failed.append((num, "image", str(exc)))
                image_ok = False
        else:
            log("  image: skipped (already exists)")

        # ---- motion clip ----
        if video_backend == "none" or not image_ok:
            continue
        out_mp4 = work / f"scene_{num}.mp4"
        if not wants_video(scene):
            log("  video: skipped (animate = no, this scene stays a still)")
            continue
        if not video_needed(out_mp4, video_backend):
            log("  video: skipped (clip already exists)")
            continue
        if video_backend == "wan" and is_placeholder(out_png):
            log("  video: skipped (the image is still a placeholder, not spending GPU on it)")
            video_missing.append(num)
            continue
        if wan_quota_hit:
            video_missing.append(num)
            log("  video: skipped (GPU quota already exhausted in this run)")
            continue
        try:
            started = time.time()
            kind = make_scene_video(scene, out_png, out_mp4, video_backend, wan, clip_seconds, wan_steps, by_name, work)
            log(f"  video: {kind} clip done in {time.time() - started:.0f}s (wall clock, includes queue time)")
        except QuotaError as exc:
            wan_quota_hit = True
            video_missing.append(num)
            log(f"GPU QUOTA EXHAUSTED at scene {num} (video): {str(exc)[:300]}")
            if require_video:
                return 3
            log("  Continuing without more clips; remaining scenes will render as stills. Re-run later to add them.")
        except Exception as exc:  # noqa: BLE001
            log(f"  VIDEO FAILED: {exc}")
            video_missing.append(num)
            out_mp4.unlink(missing_ok=True)
            if require_video:
                failed.append((num, "video", str(exc)))

    if failed:
        log("Failed scenes: " + "; ".join(f"{n} ({kind})" for n, kind, _ in failed))
        return 2
    if video_missing:
        log(f"PARTIAL: no motion clip for scene(s) {sorted(set(video_missing))}; they will render as slow-zoom stills. "
            "Re-run later with the same folder to add the clips.")
        if require_video:
            return 3
    log(f"All {total} scene(s) generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

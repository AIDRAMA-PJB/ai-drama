#!/usr/bin/env python3

"""
generate_assets.py - builds every scene's voice track and face-locked image.

Runs on the GitHub Actions runner (NOT on Render), so n8n stays light.

Inputs inside --workdir:
    characters.json
    storyboard.json

Outputs inside --workdir:
    avatars/<Name_With_Underscores>.png
    scene_<N>.mp3
    scene_<N>.png

Existing non-empty outputs are skipped, so re-running a failed job
resumes where it stopped.

Exit codes:
    0 = all done
    2 = some scenes failed
    3 = GPU quota exhausted
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NARRATOR_VOICE = "en-US-ChristopherNeural"
RADIO_DJ_VOICE = "en-US-EricNeural"
FALLBACK_VOICE = "en-US-GuyNeural"

PAUSE_SECONDS = 0.3

NEGATIVE_PROMPT = (
    "blurry, low quality, distorted face, extra limbs, "
    "watermark, deformed, text, logo"
)

IMAGE_BACKEND = os.environ.get(
    "IMAGE_BACKEND",
    "instantid"
).strip().lower()

QUOTA_HINTS = (
    "quota",
    "zerogpu",
    "exceeded your",
    "too many requests",
    "429",
    "rate limit",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class QuotaError(RuntimeError):
    """Raised when the Hugging Face Space refuses work because quota is used up."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def safe_name(name):
    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "",
        re.sub(r"\s+", "_", name.strip())
    )


def scene_num(scene):
    return int(float(str(scene["scene_number"]).strip()))


def extract_drive_id(url):
    match = (
        re.search(r"[?&]id=([A-Za-z0-9_-]+)", url or "")
        or re.search(r"/d/([A-Za-z0-9_-]+)", url or "")
    )
    return match.group(1) if match else None


def parse_scene_filter(text):
    """
    Examples:
        "1-3,7" -> {1,2,3,7}
        ""      -> None (all scenes)
    """

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


# ---------------------------------------------------------------------------
# Text -> Voice
# ---------------------------------------------------------------------------

def name_aliases(name):
    tokens = name.split()

    aliases = {name.strip()}

    if tokens:
        # "Philip Sterling" -> "Philip"
        # "Mr. Reyes" -> "Reyes"
        aliases.add(
            tokens[-1] if tokens[0].endswith(".") else tokens[0]
        )

    return {a for a in aliases if a}


def build_voice_map(characters):
    voices = {
        "narration": NARRATOR_VOICE,
        "narrator": NARRATOR_VOICE,
        "radio dj": RADIO_DJ_VOICE,
    }

    for character in characters:
        voice = (
            character.get("voice_edge_tts") or ""
        ).strip() or FALLBACK_VOICE

        for alias in name_aliases(
            character["character_name"]
        ):
            voices[alias.lower()] = voice

    return voices


def build_speaker_regex(voice_map):
    labels = sorted(
        voice_map.keys(),
        key=len,
        reverse=True
    )

    alternatives = "|".join(
        re.escape(label)
        for label in labels
    )

    return re.compile(
        rf"(?:^|(?<=\s))({alternatives})\s*:\s*",
        re.IGNORECASE
    )


def clean_speech(text):
    text = text.replace("\\!", "!")

    # Remove stage directions.
    text = re.sub(
        r"\([^)]*\)",
        " ",
        text
    )

    # Remove quote marks.
    text = re.sub(
        r"[\"\u201c\u201d]",
        "",
        text
    )

    # Convert large dollar amounts.
    text = re.sub(
        r"\$\s?(\d[\d,.]*)\s*(billion|million|thousand)\b",
        r"\1 \2 dollars",
        text,
        flags=re.I
    )

    # Convert normal dollar amounts.
    text = re.sub(
        r"\$\s?(\d[\d,.]*)",
        r"\1 dollars",
        text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def split_segments(text, rx):
    """
    Example:
        Narration: A
        Philip: B

    becomes:
        [
            ("Narration", "A"),
            ("Philip", "B")
        ]
    """

    text = str(text or "").strip()

    matches = list(
        rx.finditer(text)
    )

    if not matches:
        cleaned = clean_speech(text)

        if cleaned:
            return [("Narration", cleaned)]

        return []

    segments = []

    lead = text[
        :matches[0].start()
    ].strip()

    if lead:
        segments.append(
            ("Narration", clean_speech(lead))
        )

    for i, match in enumerate(matches):
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        segments.append(
            (
                match.group(1),
                clean_speech(
                    text[match.end():end]
                )
            )
        )

    return [
        (label, text)
        for label, text in segments
        if text
    ]


async def _synth(text, voice, out_path):
    import edge_tts

    await edge_tts.Communicate(
        text,
        voice
    ).save(str(out_path))


def synth_with_retry(
    text,
    voice,
    out_path,
    attempts=3
):
    for i in range(1, attempts + 1):

        try:
            asyncio.run(
                _synth(
                    text,
                    voice,
                    out_path
                )
            )

            if (
                Path(out_path).exists()
                and Path(out_path).stat().st_size > 0
            ):
                return

            raise RuntimeError(
                "edge-tts produced an empty file"
            )

        except Exception as exc:

            if i == attempts:
                raise

            log(
                f"    TTS retry "
                f"{i}/{attempts - 1} after: {exc}"
            )

            time.sleep(5 * i)


def make_scene_audio(
    scene,
    rx,
    voice_map,
    workdir
):
    num = scene_num(scene)

    out = workdir / f"scene_{num}.mp3"

    if non_empty(out):
        return "skipped"

    segments = split_segments(
        scene["dialogue_or_narration"],
        rx
    )

    if not segments:
        raise RuntimeError(
            f"scene {num}: no speakable text"
        )

    tmp = workdir / f"_tts_{num}"

    tmp.mkdir(
        exist_ok=True
    )

    parts = []

    for i, (label, text) in enumerate(segments):

        voice = voice_map.get(
            label.lower(),
            FALLBACK_VOICE
        )

        part = tmp / f"seg_{i}.mp3"

        synth_with_retry(
            text,
            voice,
            part
        )

        parts.append(part)

    if len(parts) == 1:

        shutil.move(
            str(parts[0]),
            out
        )

    else:

        silence = workdir / "_silence.mp3"

        if not non_empty(silence):

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=24000:cl=mono",
                    "-t",
                    str(PAUSE_SECONDS),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "48k",
                    str(silence),
                ],
                check=True
            )

        lines = []

        for i, part in enumerate(parts):

            if i:
                lines.append(
                    f"file '{silence.resolve()}'"
                )

            lines.append(
                f"file '{part.resolve()}'"
            )

        list_file = tmp / "list.txt"

        list_file.write_text(
            "\n".join(lines) + "\n"
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "96k",
                str(out),
            ],
            check=True
        )

    shutil.rmtree(
        tmp,
        ignore_errors=True
    )

    return f"{len(segments)} segment(s)"


# ---------------------------------------------------------------------------
# Avatar handling
# ---------------------------------------------------------------------------

def looks_like_image(path):
    if not path.exists():
        return False

    head = path.read_bytes()[:12]

    return (
        head.startswith(b"\x89PNG")
        or head.startswith(b"\xff\xd8")
        or head[:4] == b"RIFF"
    )


def fetch_avatar(
    character,
    dest_dir
):
    name = character["character_name"].strip()

    dest = (
        dest_dir
        / f"{safe_name(name)}.png"
    )

    # Reuse existing avatar.
    if (
        non_empty(dest)
        and looks_like_image(dest)
    ):
        return dest

    dest.unlink(
        missing_ok=True
    )

    url = character.get(
        "avatar_reference_url",
        ""
    )

    file_id = extract_drive_id(url)

    if not file_id:
        raise RuntimeError(
            f"{name}: avatar_reference_url "
            f"has no Drive file id"
        )

    # First attempt:
    # authenticated Google Drive copy using rclone.
    result = subprocess.run(
        [
            "rclone",
            "backend",
            "copyid",
            "gdrive:",
            file_id,
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=180
    )

    if (
        result.returncode == 0
        and non_empty(dest)
        and looks_like_image(dest)
    ):
        return dest

    log(
        f"  rclone could not fetch "
        f"{name}'s avatar "
        f"({result.stderr.strip()[:160]}); "
        f"trying the public link"
    )

    # Second attempt:
    # public Google Drive link.
    import httpx

    response = httpx.get(
        url,
        follow_redirects=True,
        timeout=90
    )

    dest.write_bytes(
        response.content
    )

    if not looks_like_image(dest):

        dest.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            f"{name}: avatar download did not "
            f"return an image "
            f"(is the Drive file shared with "
            f"this account?)"
        )

    return dest


# ---------------------------------------------------------------------------
# InstantID
# ---------------------------------------------------------------------------

def pick_primary(
    scene,
    characters_by_name
):
    """
    Face to lock = the listed character that
    the image prompt mentions first.
    """

    names = [
        n.strip()
        for n in str(
            scene.get(
                "characters_in_scene",
                ""
            )
        ).split(",")
        if n.strip()
    ]

    if not names:
        raise RuntimeError(
            f"scene {scene_num(scene)}: "
            f"characters_in_scene is empty"
        )

    prompt = str(
        scene.get(
            "visual_prompt_en",
            ""
        )
    )

    found = sorted(
        (
            prompt.find(name),
            name
        )
        for name in names
        if name in prompt
    )

    chosen = (
        found[0][1]
        if found
        else names[0]
    )

    if chosen not in characters_by_name:
        raise RuntimeError(
            f"scene {scene_num(scene)}: "
            f"unknown character '{chosen}'"
        )

    return chosen


def build_kwargs(
    params,
    face_path,
    prompt,
    seed,
    handle_file
):
    """
    Build only parameters currently exposed
    by the InstantID Space.
    """

    known = {
        "face_image_path":
            lambda: handle_file(str(face_path)),

        "pose_image_path":
            lambda: None,

        "prompt":
            lambda: prompt,

        "negative_prompt":
            lambda: NEGATIVE_PROMPT,

        "style_name":
            lambda: "(No style)",

        "num_steps":
            lambda: 4,

        "identitynet_strength_ratio":
            lambda: 0.8,

        "adapter_strength_ratio":
            lambda: 0.8,

        "canny_strength":
            lambda: 0.3,

        "depth_strength":
            lambda: 0.4,

        "controlnet_selection":
            lambda: ["depth"],

        "guidance_scale":
            lambda: 1.0,

        "seed":
            lambda: seed,

        "scheduler":
            lambda: "EulerDiscreteScheduler",

        "enable_lcm":
            lambda: True,

        "enhance_face_region":
            lambda: True,
    }

    kwargs = {}
    missing = []

    for parameter in params:

        parameter_name = parameter[
            "parameter_name"
        ]

        if parameter_name in known:

            kwargs[parameter_name] = (
                known[parameter_name]()
            )

        elif not parameter.get(
            "parameter_has_default"
        ):

            missing.append(
                parameter_name
            )

    if missing:
        raise RuntimeError(
            "The Space API changed; "
            "unknown required parameters: "
            + ", ".join(missing)
        )

    if (
        "face_image_path" not in kwargs
        or "prompt" not in kwargs
    ):
        raise RuntimeError(
            "The Space API changed; "
            "no face_image_path/prompt "
            "parameter found"
        )

    return kwargs


def extract_result_path(result):
    r = result

    while isinstance(
        r,
        (list, tuple)
    ) and r:
        r = r[0]

    if isinstance(r, dict):

        r = (
            r.get("path")
            or r.get("value")
            or r.get("name")
        )

        if isinstance(r, dict):
            r = r.get("path")

    if (
        not isinstance(r, str)
        or not Path(r).exists()
    ):
        raise RuntimeError(
            "unexpected result from the Space: "
            + str(result)[:200]
        )

    return r


class InstantIDClient:

    def __init__(self):

        self.client = None
        self.endpoint = None
        self.params = None
        self.handle_file = None

    def connect(self):

        from gradio_client import (
            Client,
            handle_file
        )

        self.handle_file = handle_file

        space = os.environ.get(
            "INSTANTID_SPACE",
            "InstantX/InstantID"
        )

        token = (
            os.environ.get(
                "HF_TOKEN"
            ) or ""
        ).strip() or None

        log(
            f"Connecting to Hugging Face Space "
            f"{space} "
            f"({'with' if token else 'without'} token)"
        )

        if token:

            try:
                self.client = Client(
                    space,
                    token=token
                )

            except TypeError:

                # Older gradio_client versions.
                self.client = Client(
                    space,
                    hf_token=token
                )

        else:

            self.client = Client(
                space
            )

        api = self.client.view_api(
            return_format="dict"
        )

        endpoints = api.get(
            "named_endpoints",
            {}
        )

        endpoint_name = (
            "/generate_image"
            if "/generate_image" in endpoints
            else next(
                (
                    key
                    for key in endpoints
                    if "generate" in key
                ),
                None
            )
        )

        if not endpoint_name:
            raise RuntimeError(
                "The Space has no generate endpoint; "
                "available: "
                + ", ".join(endpoints)
            )

        self.endpoint = endpoint_name

        self.params = endpoints[
            endpoint_name
        ]["parameters"]

        log(
            "Using endpoint "
            f"{endpoint_name} with parameters: "
            + ", ".join(
                p["parameter_name"]
                for p in self.params
            )
        )

    def generate(
        self,
        face_path,
        prompt,
        seed,
        out_path
    ):

        if self.client is None:
            self.connect()

        kwargs = build_kwargs(
            self.params,
            face_path,
            prompt,
            seed,
            self.handle_file
        )

        delays = [
            20,
            60,
            120
        ]

        for attempt in range(
            len(delays) + 1
        ):

            try:

                result = self.client.predict(
                    api_name=self.endpoint,
                    **kwargs
                )

                result_path = (
                    extract_result_path(
                        result
                    )
                )

                shutil.copy(
                    result_path,
                    out_path
                )

                return

            except Exception as exc:

                text = str(exc).lower()

                if any(
                    hint in text
                    for hint in QUOTA_HINTS
                ):
                    raise QuotaError(
                        str(exc)
                    ) from exc

                if attempt == len(delays):
                    raise

                log(
                    f"    image retry "
                    f"{attempt + 1}/"
                    f"{len(delays)} "
                    f"in {delays[attempt]}s "
                    f"after: "
                    f"{str(exc)[:200]}"
                )

                time.sleep(
                    delays[attempt]
                )


# ---------------------------------------------------------------------------
# Placeholder image
# ---------------------------------------------------------------------------

def generate_placeholder_image(
    scene,
    out_path
):
    """
    Generate a simple black placeholder image.

    This is only for testing the complete:
        TTS -> FFmpeg -> Drive -> callback
    pipeline without using Hugging Face GPU quota.
    """

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1080x1920",
            "-frames:v",
            "1",
            str(out_path),
        ],
        check=True
    )

    if not non_empty(out_path):
        raise RuntimeError(
            "placeholder image was not created"
        )

    log(
        "  image: placeholder generated"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--workdir",
        required=True
    )

    ap.add_argument(
        "--scenes",
        default="",
        help=(
            "optional filter, e.g. "
            "'1-3,7' "
            "(default: all scenes)"
        )
    )

    args = ap.parse_args(argv)

    work = Path(
        args.workdir
    )

    # -----------------------------------------------------------------------
    # Load input JSON files
    # -----------------------------------------------------------------------

    characters = json.loads(
        (
            work / "characters.json"
        ).read_text(
            encoding="utf-8"
        )
    )

    scenes = json.loads(
        (
            work / "storyboard.json"
        ).read_text(
            encoding="utf-8"
        )
    )

    scenes = sorted(
        scenes,
        key=scene_num
    )

    wanted = parse_scene_filter(
        args.scenes
    )

    if wanted is not None:

        scenes = [
            scene
            for scene in scenes
            if scene_num(scene) in wanted
        ]

    if not scenes:

        log(
            "No scenes selected."
        )

        return 2

    # -----------------------------------------------------------------------
    # Character / voice setup
    # -----------------------------------------------------------------------

    by_name = {
        character["character_name"].strip():
            character
        for character in characters
    }

    voice_map = build_voice_map(
        characters
    )

    rx = build_speaker_regex(
        voice_map
    )

    # -----------------------------------------------------------------------
    # IMPORTANT:
    #
    # Placeholder mode does NOT need:
    #   - avatar downloads
    #   - Hugging Face
    #   - InstantID
    #
    # InstantID mode does.
    # -----------------------------------------------------------------------

    avatars = {}
    instantid = None

    if IMAGE_BACKEND == "instantid":

        avatars_dir = (
            work / "avatars"
        )

        avatars_dir.mkdir(
            exist_ok=True
        )

        needed = {
            pick_primary(
                scene,
                by_name
            )
            for scene in scenes
        }

        for name in sorted(
            needed
        ):

            avatars[name] = fetch_avatar(
                by_name[name],
                avatars_dir
            )

            log(
                f"Avatar ready: {name}"
            )

        instantid = InstantIDClient()

    elif IMAGE_BACKEND == "placeholder":

        log(
            "IMAGE_BACKEND=placeholder "
            "-> skipping avatar downloads "
            "and Hugging Face."
        )

    else:

        raise RuntimeError(
            f"Unknown IMAGE_BACKEND: "
            f"{IMAGE_BACKEND}. "
            f"Use 'instantid' or 'placeholder'."
        )

    # -----------------------------------------------------------------------
    # Process scenes
    # -----------------------------------------------------------------------

    failed = []

    total = len(
        scenes
    )

    for idx, scene in enumerate(
        scenes,
        1
    ):

        num = scene_num(
            scene
        )

        log(
            f"Scene {num} "
            f"({idx}/{total})"
        )

        # ---------------------------------------------------------------
        # Generate voice
        # ---------------------------------------------------------------

        try:

            voice_result = make_scene_audio(
                scene,
                rx,
                voice_map,
                work
            )

            log(
                f"  voice: "
                f"{voice_result}"
            )

        except Exception as exc:

            log(
                f"  VOICE FAILED: "
                f"{exc}"
            )

            failed.append(
                (
                    num,
                    "voice",
                    str(exc)
                )
            )

            continue

        # ---------------------------------------------------------------
        # Check existing image
        # ---------------------------------------------------------------

        out_png = (
            work
            / f"scene_{num}.png"
        )

        if non_empty(out_png):

            log(
                "  image: skipped "
                "(already exists)"
            )

            continue

        # ---------------------------------------------------------------
        # Generate image
        # ---------------------------------------------------------------

        try:

            if IMAGE_BACKEND == "placeholder":

                generate_placeholder_image(
                    scene,
                    out_png
                )

            elif IMAGE_BACKEND == "instantid":

                primary = pick_primary(
                    scene,
                    by_name
                )

                seed = int(
                    float(
                        by_name[
                            primary
                        ].get(
                            "avatar_seed"
                        )
                        or 42
                    )
                )

                prompt = re.sub(
                    r"\s+",
                    " ",
                    str(
                        scene[
                            "visual_prompt_en"
                        ]
                    )
                ).strip()

                log(
                    f"  image: "
                    f"face lock = "
                    f"{primary}, "
                    f"seed = {seed}"
                )

                instantid.generate(
                    avatars[
                        primary
                    ],
                    prompt,
                    seed,
                    out_png
                )

                log(
                    "  image: done"
                )

        except QuotaError as exc:

            log(
                f"GPU QUOTA EXHAUSTED "
                f"at scene {num}: "
                f"{str(exc)[:300]}"
            )

            log(
                "Add a Hugging Face token "
                "as the HF_TOKEN repo secret, "
                "or re-run later. "
                "Finished scenes are kept."
            )

            return 3

        except Exception as exc:

            log(
                f"  IMAGE FAILED: "
                f"{exc}"
            )

            failed.append(
                (
                    num,
                    "image",
                    str(exc)
                )
            )

    # -----------------------------------------------------------------------
    # Final status
    # -----------------------------------------------------------------------

    if failed:

        log(
            "Failed scenes: "
            + "; ".join(
                f"{number} ({kind})"
                for number, kind, _ in failed
            )
        )

        return 2

    log(
        f"All {total} scene(s) generated."
    )

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(
        main()
    )

#!/opt/venv/bin/python3
"""
instantid_generate.py — Generates a scene image with a character's face locked in,
using the free InstantID Hugging Face Space via gradio_client.

Usage:
  python3 instantid_generate.py <reference_face_image_path> <scene_prompt> <output_path>

IMPORTANT: Community Hugging Face Spaces occasionally change their exact API parameter
names when the Space owner updates the app. Before relying on this in production, open
https://huggingface.co/spaces/InstantX/InstantID in a browser, scroll to the bottom,
click "Use via API", and confirm the parameter names below still match. If they don't,
update the client.predict(...) call accordingly — the rest of this script (reading args,
saving output) does not need to change.
"""
import sys
import os
import shutil
from gradio_client import Client, handle_file


def main():
    if len(sys.argv) < 4:
        print("Usage: instantid_generate.py <face_image_path> <prompt> <output_path>")
        sys.exit(1)

    face_image_path, prompt, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    client = Client("InstantX/InstantID")  # public, free ZeroGPU Space

    # Matches the *current* generate_image() signature in InstantX/InstantID's app.py
    # (verified against the live source on 2026-09-20). Note: pose_strength does NOT
    # exist anymore — it's commented out in the Space's own code. depth_strength DOES
    # exist and is required. If this breaks again later, re-check the "inputs=[...]"
    # list inside the submit.click(...).then(fn=generate_image, inputs=[...]) block at
    # https://huggingface.co/spaces/InstantX/InstantID/blob/main/app.py — that list is
    # the authoritative parameter order/names, more reliable than the "Use via API" page.
    try:
        result = client.predict(
            face_image_path=handle_file(face_image_path),
            pose_image_path=None,
            prompt=prompt,
            negative_prompt=(
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, "
                "(frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, "
                "blurry, deformed, monochrome, gun, weapon"
            ),
            style_name="(No style)",
            num_steps=20,
            identitynet_strength_ratio=0.8,
            adapter_strength_ratio=0.8,
            canny_strength=0.4,
            depth_strength=0.4,
            controlnet_selection=["depth"],
            guidance_scale=5.0,
            seed=42,
            scheduler="EulerDiscreteScheduler",
            enable_LCM=False,
            enhance_face_region=True,
            api_name="/generate_image",
        )
    except Exception as e:
        print(f"ERROR calling InstantID Space: {e}", file=sys.stderr)
        sys.exit(1)

    # generate_image() returns (image, usage_tips_update) — first element is the image.
    # gradio_client may hand this back as a plain filepath string or as a dict with a
    # 'path' key depending on version; handle both.
    first_output = result[0] if isinstance(result, (list, tuple)) else result
    if isinstance(first_output, dict):
        image_path = first_output.get("path") or first_output.get("url")
    else:
        image_path = first_output

    if not image_path or not os.path.exists(image_path):
        print(f"ERROR: no valid image returned. Raw result: {result}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    shutil.copy(image_path, output_path)
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()

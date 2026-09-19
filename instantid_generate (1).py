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

    result = client.predict(
        face_image_path=handle_file(face_image_path),
        pose_image_path=None,
        prompt=prompt,
        negative_prompt=(
            "blurry, low quality, distorted face, extra limbs, watermark, deformed"
        ),
        style_name="(No style)",
        num_steps=20,
        identitynet_strength_ratio=0.85,
        adapter_strength_ratio=0.75,
        pose_strength=0.3,
        canny_strength=0.3,
        controlnet_selection=[],
        guidance_scale=5.0,
        seed=42,
        scheduler="EulerDiscreteScheduler",
        enable_LCM=False,
        enhance_face_region=True,
        api_name="/generate_image",
    )

    # Result shape can vary (image path, or [image_path, seed]); handle both.
    image_path = result[0] if isinstance(result, (list, tuple)) else result

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    shutil.copy(image_path, output_path)
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()

#!/bin/bash
# render.sh - assembles scene images + voice audio into one final drama video.
# Used by the GitHub Actions workflow (render.yml) and by the "Assemble Video (FFmpeg)" node in n8n.
#
# Usage: ./render.sh /path/to/work_folder
#
# Expected inside the folder:
#   scene_1.png, scene_2.png, ...   -> AI-generated image per scene
#   scene_1.mp3, scene_2.mp3, ...   -> TTS audio per scene (edge-tts)
#   bgm.mp3                         -> optional background music (if missing, the no-BGM branch runs)
#
# Output: final.mp4 in the same folder (1080x1920, portrait, for Shorts/Reels/TikTok)

set -e
DIR="$1"

if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
  echo "ERROR: valid folder path required. Usage: ./render.sh /path/to/work_folder"
  exit 1
fi

cd "$DIR"
rm -f concat_list.txt
rm -f clip_*.mp4

# 1) Build a Ken Burns (slow zoom) clip per scene, lasting as long as its narration.
#    Scenes are processed in numeric order (scene_2 before scene_10).
for img in $(ls scene_*.png 2>/dev/null | sort -V); do
  [ -e "$img" ] || continue
  num="${img#scene_}"
  num="${num%.png}"
  audio="scene_${num}.mp3"

  if [ ! -f "$audio" ]; then
    echo "WARNING: no audio found for $img, skipping."
    continue
  fi

  dur=$(ffprobe -v error -show_entries format=duration -of csv="p=0" "$audio")
  frames=$(awk -v d="$dur" 'BEGIN{printf "%d", d*25}')

  ffmpeg -y -loop 1 -i "$img" -i "$audio" \
    -filter_complex "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,zoompan=z='min(zoom+0.0015,1.3)':d=${frames}:s=1080x1920:fps=25[v]" \
    -map "[v]" -map 1:a -t "$dur" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
    "clip_${num}.mp4"

  echo "file 'clip_${num}.mp4'" >> concat_list.txt
done

if [ ! -s concat_list.txt ]; then
  echo "ERROR: no clips were generated. Check that the scene_N.png / scene_N.mp3 naming is correct."
  exit 1
fi

# 2) Concatenate all scene clips, then mix in the background music (if present)
if [ -f "bgm.mp3" ]; then
  ffmpeg -y -f concat -safe 0 -i concat_list.txt -stream_loop -1 -i bgm.mp3 \
    -filter_complex "[1:a]volume=0.15[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]" \
    -map 0:v -map "[aout]" -c:v libx264 -preset veryfast -pix_fmt yuv420p -shortest \
    final.mp4
else
  ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy final.mp4
fi

echo "DONE: ${DIR}/final.mp4"

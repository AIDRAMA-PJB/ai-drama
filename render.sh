#!/bin/bash
# render.sh - assembles scene images (or motion clips) + voice audio into one final drama video.
# Used by the GitHub Actions workflow (render.yml).
#
# Usage: ./render.sh /path/to/work_folder
#
# Expected inside the folder:
#   scene_1.png, scene_2.png, ...   -> AI-generated image per scene
#   scene_1.mp3, scene_2.mp3, ...   -> voice audio per scene
#   scene_1.mp4, ...                -> OPTIONAL motion clip per scene (Wan image-to-video, ~3-5 s)
#   bgm.mp3                         -> optional background music (if missing, the no-BGM branch runs)
#
# Per scene:
#   - with scene_N.mp4: the moving clip plays first; if the narration is longer than the clip, the last frame
#     continues with a slow zoom until the audio ends; if the clip is longer, it is cut at the audio length.
#   - without it: the still image gets a Ken Burns (slow zoom) for the whole narration.
# Output: final.mp4 in the same folder (1080x1920, 25 fps, portrait, for Shorts/Reels/TikTok)

set -e
DIR="$1"

if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
  echo "ERROR: valid folder path required. Usage: ./render.sh /path/to/work_folder"
  exit 1
fi

cd "$DIR"
rm -f concat_list.txt
rm -f clip_*.mp4 last_*.png

FIT="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

# 1) Build one clip per scene, lasting as long as its narration. Scenes are processed in numeric order.
for img in $(ls scene_*.png 2>/dev/null | sort -V); do
  [ -e "$img" ] || continue
  num="${img#scene_}"
  num="${num%.png}"
  audio="scene_${num}.mp3"
  motion="scene_${num}.mp4"

  if [ ! -f "$audio" ]; then
    echo "WARNING: no audio found for $img, skipping."
    continue
  fi

  dur=$(ffprobe -v error -show_entries format=duration -of csv="p=0" "$audio")

  mdur=""
  if [ -s "$motion" ]; then
    mdur=$(ffprobe -v error -show_entries format=duration -of csv="p=0" "$motion" 2>/dev/null || true)
    if ! awk -v d="${mdur:-0}" 'BEGIN{exit !(d>0.3)}'; then
      echo "WARNING: $motion is not a valid clip, using the still image instead."
      mdur=""
    fi
  fi

  if [ -n "$mdur" ] && awk -v m="$mdur" -v d="$dur" 'BEGIN{exit !(m>=d)}'; then
    # motion clip is at least as long as the narration: cut it to the audio length
    echo "Scene ${num}: motion clip (${mdur}s) cut to narration (${dur}s)"
    ffmpeg -y -loglevel error -i "$motion" -i "$audio" \
      -filter_complex "[0:v]${FIT},fps=25,setsar=1,format=yuv420p[v]" \
      -map "[v]" -map 1:a -t "$dur" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
      "clip_${num}.mp4"

  elif [ -n "$mdur" ] && awk -v m="$mdur" -v d="$dur" 'BEGIN{exit !(d-m<0.15)}'; then
    # almost the same length: no tail needed
    echo "Scene ${num}: motion clip (${mdur}s) matches narration (${dur}s)"
    ffmpeg -y -loglevel error -i "$motion" -i "$audio" \
      -filter_complex "[0:v]${FIT},fps=25,setsar=1,format=yuv420p[v]" \
      -map "[v]" -map 1:a -t "$dur" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
      "clip_${num}.mp4"

  elif [ -n "$mdur" ]; then
    # narration is longer than the motion clip: play the clip, then keep going from its last frame with a slow zoom
    tail=$(awk -v d="$dur" -v m="$mdur" 'BEGIN{printf "%.3f", d-m}')
    tframes=$(awk -v t="$tail" 'BEGIN{printf "%d", t*25+2}')
    echo "Scene ${num}: motion clip (${mdur}s) + ${tail}s slow-zoom hold to reach narration (${dur}s)"
    ffmpeg -y -loglevel error -sseof -0.3 -i "$motion" -update 1 -frames:v 1 "last_${num}.png"
    ffmpeg -y -loglevel error -i "$motion" -loop 1 -t "$tail" -i "last_${num}.png" -i "$audio" \
      -filter_complex "[0:v]${FIT},fps=25,setsar=1,format=yuv420p[m];[1:v]${FIT},zoompan=z='min(zoom+0.0008,1.15)':d=${tframes}:s=1080x1920:fps=25,setsar=1,format=yuv420p[t];[m][t]concat=n=2:v=1:a=0[v]" \
      -map "[v]" -map 2:a -t "$dur" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
      "clip_${num}.mp4"

  else
    # no motion clip: Ken Burns (slow zoom) on the still image for the whole narration
    frames=$(awk -v d="$dur" 'BEGIN{printf "%d", d*25}')
    ffmpeg -y -loglevel error -loop 1 -i "$img" -i "$audio" \
      -filter_complex "[0:v]${FIT},zoompan=z='min(zoom+0.0015,1.3)':d=${frames}:s=1080x1920:fps=25,setsar=1,format=yuv420p[v]" \
      -map "[v]" -map 1:a -t "$dur" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
      "clip_${num}.mp4"
  fi

  echo "file 'clip_${num}.mp4'" >> concat_list.txt
done

if [ ! -s concat_list.txt ]; then
  echo "ERROR: no clips were generated. Check that the scene_N.png / scene_N.mp3 naming is correct."
  exit 1
fi

# 2) Concatenate all scene clips, then mix in the background music (if present)
if [ -f "bgm.mp3" ]; then
  ffmpeg -y -loglevel error -f concat -safe 0 -i concat_list.txt -stream_loop -1 -i bgm.mp3 \
    -filter_complex "[1:a]volume=0.15[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]" \
    -map 0:v -map "[aout]" -c:v libx264 -preset veryfast -pix_fmt yuv420p -shortest \
    final.mp4
else
  ffmpeg -y -loglevel error -f concat -safe 0 -i concat_list.txt -c copy final.mp4
fi

echo "DONE: ${DIR}/final.mp4"

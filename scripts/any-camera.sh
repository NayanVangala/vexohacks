#!/bin/sh
# Turn any camera into a Scene device: a laptop webcam, a USB camera, a Raspberry Pi,
# an open-source smart-glasses dev kit. Scene's device API is two plain HTTP calls:
#
#   POST /capture   multipart `photo` = one JPEG frame      (every ~2.5 s)
#   POST /capture   multipart `audio` = one WAV chunk (16 kHz mono, ~2.5 s)
#
# That's the whole contract the Meta Ray-Ban bridge speaks too. This script does it
# with ffmpeg + curl so you can see a second device type working in a minute.
#
# Usage:
#   scripts/any-camera.sh                          # Mac webcam + mic -> http://localhost:8001
#   SCENE_URL=https://xyz.trycloudflare.com scripts/any-camera.sh
#   SOURCE=test scripts/any-camera.sh              # synthetic frames, no camera needed
#   CAMERA=1 MIC=0 scripts/any-camera.sh           # pick devices (ffmpeg -f avfoundation -list_devices true -i "")
#
# On Linux / Raspberry Pi, set INPUT to an ffmpeg input, e.g.
#   INPUT="-f v4l2 -i /dev/video0" AUDIO_INPUT="-f alsa -i default" scripts/any-camera.sh

URL="${SCENE_URL:-http://localhost:8001}"
EVERY="${EVERY:-2.5}"
CAMERA="${CAMERA:-0}"
MIC="${MIC:-0}"
TMP="$(mktemp -d)"
# Stop everything this script started (the audio loop and its ffmpeg too), not just the main loop.
trap 'trap - EXIT INT TERM; rm -rf "$TMP"; kill 0 2>/dev/null' EXIT INT TERM

command -v ffmpeg >/dev/null || { echo "Needs ffmpeg (brew install ffmpeg)."; exit 1; }
curl -s -m 5 "$URL/stats" >/dev/null || { echo "Can't reach Scene at $URL. Start it first (scripts/start-scene.command)."; exit 1; }

if [ "$SOURCE" = "test" ]; then
  VIDEO_IN="-f lavfi -i testsrc2=size=1280x720:rate=1"
  AUDIO_IN="-re -f lavfi -i sine=frequency=440:sample_rate=16000"   # -re: real time, like a mic
elif [ -n "$INPUT" ]; then
  VIDEO_IN="$INPUT"
  AUDIO_IN="${AUDIO_INPUT:-}"
else
  VIDEO_IN="-f avfoundation -framerate 30 -i $CAMERA:none"
  AUDIO_IN="-f avfoundation -i none:$MIC"
fi

echo "Scene device: sending a frame every ${EVERY}s and audio in 2.5 s chunks to $URL (Ctrl+C to stop)"

# Audio: record 2.5 s WAV chunks back to back and post each one.
if [ -n "$AUDIO_IN" ]; then
  ( n=0
    while true; do
      n=$((n + 1))
      # shellcheck disable=SC2086
      ffmpeg -v error -y $AUDIO_IN -t 2.5 -ac 1 -ar 16000 -c:a pcm_s16le "$TMP/a.wav" </dev/null \
        && curl -s -m 10 -F "audio=@$TMP/a.wav;type=audio/wav" "$URL/capture" >/dev/null \
        && printf '  audio #%s\n' "$n"
    done ) &
  AUDIO_LOOP=$!
fi

# Video: grab one frame, post it, wait, repeat.
n=0
while true; do
  n=$((n + 1))
  # shellcheck disable=SC2086
  if ffmpeg -v error -y $VIDEO_IN -frames:v 1 -q:v 4 "$TMP/f.jpg" </dev/null; then
    curl -s -m 10 -F "photo=@$TMP/f.jpg;type=image/jpeg" "$URL/capture" >/dev/null && printf 'frame #%s\n' "$n"
  else
    echo "  camera capture failed. On macOS, allow Terminal in Settings > Privacy & Security > Camera."
  fi
  sleep "$EVERY"
done

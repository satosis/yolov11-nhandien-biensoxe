#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  cat <<'USAGE'
Usage:
  scripts/restream_tripwire.sh <RTSP_INPUT> <RTSP_OUTPUT> [line_ratio] [line_thickness]

Example:
  scripts/restream_tripwire.sh \
    "rtsp://USER:PASS@10.0.0.8:554/cam/realmonitor?channel=1&subtype=0" \
    "rtsp://0.0.0.0:8554/cam_doorline" \
    0.62 6
USAGE
  exit 1
fi

INPUT_RTSP="$1"
OUTPUT_RTSP="$2"
LINE_RATIO="${3:-0.62}"
LINE_THICKNESS="${4:-6}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "❌ ffmpeg chưa được cài. Hãy cài ffmpeg trước khi chạy script này." >&2
  exit 2
fi

exec ffmpeg -hide_banner -loglevel warning \
  -rtsp_transport tcp -i "$INPUT_RTSP" \
  -vf "drawbox=x=0:y=ih*${LINE_RATIO}:w=iw:h=${LINE_THICKNESS}:color=red@0.85:t=fill" \
  -c:v libx264 -preset veryfast -tune zerolatency -g 30 -an \
  -f rtsp "$OUTPUT_RTSP"

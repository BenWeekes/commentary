#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-10080}"
INPUT="${ROOT_DIR}/clips/bmg_fch_demo_5min/source.mp4"
URL="srt://:${PORT}?mode=listener&latency=200000&pkt_size=1316"

exec ffmpeg \
  -hide_banner \
  -re \
  -stream_loop -1 \
  -i "${INPUT}" \
  -map 0:v:0 \
  -map 0:a:0 \
  -c:v copy \
  -bsf:v h264_mp4toannexb \
  -c:a copy \
  -muxdelay 0 \
  -muxpreload 0 \
  -f mpegts \
  "${URL}"

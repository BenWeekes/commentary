#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-10080}"
INPUT="${ROOT_DIR}/clips/bmg_fch_demo_5min/source.mp4"
ATMOS="${ROOT_DIR}/clips/bmg_fch_demo_5min/atmosphere.wav"
URL="srt://:${PORT}?mode=listener&latency=200000&pkt_size=1316"

exec ffmpeg \
  -hide_banner \
  -re \
  -stream_loop -1 \
  -i "${INPUT}" \
  -stream_loop -1 \
  -i "${ATMOS}" \
  -map 0:v:0 \
  -map 1:a:0 \
  -map 0:a:0 \
  -c:v copy \
  -bsf:v h264_mp4toannexb \
  -c:a aac \
  -ar:a 16000 \
  -ac:a 1 \
  -b:a 64k \
  -muxdelay 0 \
  -muxpreload 0 \
  -f mpegts \
  "${URL}"

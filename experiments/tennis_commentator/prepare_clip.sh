#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="/home/ubuntu/tennis_challenger_atp_challenger_cary_usa_men_singles_8960445_512k.mp4"
PY="$BASE/../../.venv/bin/python"
OUT="$("$PY" -c "from tennis_common import CLIP; print(CLIP)")"
VERSIONS="$("$PY" -c "from tennis_common import INPUT_VERSION, VERSION; print(INPUT_VERSION, VERSION)")"
read -r INPUT_VERSION VERSION <<<"$VERSIONS"

if [[ "$INPUT_VERSION" != "$VERSION" ]]; then
  echo "refusing to overwrite immutable reused input artifacts ($VERSIONS)" >&2
  exit 1
fi

"$PY" -c \
  "from tennis_common import assert_football_idle; assert_football_idle()"
test -r "$SOURCE"
mkdir -p "$(dirname "$OUT")"

# Re-encode so clip time zero is exact and seek/scrub behavior is deterministic.
ffmpeg -hide_banner -loglevel warning -ss 02:00:15 -i "$SOURCE" -t 300 \
  -map 0:v:0 -map 0:a:0 -c:v libx264 -preset veryfast -crf 20 \
  -c:a aac -b:a 128k -movflags +faststart -y "$OUT"

DURATION="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")"
python3 - "$DURATION" <<'PY'
import sys
duration = float(sys.argv[1])
if not 299.8 <= duration <= 300.2:
    raise SystemExit(f"clip duration is {duration:.3f}s, expected 300s")
PY
echo "prepared $OUT ($DURATION seconds)"

#!/bin/bash
# 6-second, vision/tracker-only (no STT) variant -> /blend_v4_6s_vt/
# Alex's request: same 6s fast profile, but commentary driven only by vision + tracker.
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4

echo "[1/3] live run — 6s fast profile, STT OFF ($(date -u +%H:%M:%S))"
BLEND_DELAY_S=6 BLEND_MODE=eager USE_STT=0 "$VENV" run_blend_true_live.py

echo "[2/3] mux en/fr/pt over source video ($(date -u +%H:%M:%S))"
for L in en fr pt; do
  "$VENV" mux_with_crowd.py "$SRC" \
    "ai_blend_live_${L}_eager_6s_vt_track.wav" \
    "blend_eager_${L}_6s_vt_synced.mp4"
done

echo "[3/3] build page /blend_v4_6s_vt/ ($(date -u +%H:%M:%S))"
PAGE_LAYOUT=v4 PAGE_VERSION=v4_6s_vt FEEDBACK_VERSION=v4 \
  BLEND_ARTIFACT_SUFFIX=_6s_vt CLIP_ID=mainz_union_md33_76-81 \
  "$VENV" build_hybrid_page.py

echo "DONE $(date -u +%H:%M:%S) -> https://sa-dev.agora.io/experiments/ai_commentator/blend_v4_6s_vt/"

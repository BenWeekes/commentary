#!/usr/bin/env bash
# Package experiments/ai_commentator source + eval data (no MP4/WAV) for a new
# GPU box. Skips generated media (regeneratable from JSONL + source + TTS re-run).
#
# Usage:
#   ./migrate_to_gpu.sh                       # creates /tmp/ai_commentator_src.tgz
#   ./migrate_to_gpu.sh user@newbox:~/        # also scp to destination
set -euo pipefail

REPO_ROOT="/home/ubuntu/commentary"
OUT="/tmp/ai_commentator_src.tgz"

cd "$REPO_ROOT"

echo "=== packing ==="
# Everything in experiments/ai_commentator/ except large media
tar czf "$OUT" \
    --exclude='*.wav' \
    --exclude='*.mp4' \
    --exclude='__pycache__' \
    --exclude='.venv' \
    experiments/ai_commentator/ \
    match_data/m05_uni_md33/ \
    docs/ai/L1/10_experiments.md \
    docs/ai/L1/09_deployment.md \
    plan_tracking.md \
    AGENTS.md \
    .env.example 2>/dev/null || true

ls -lh "$OUT"

# Additionally offer to copy the 5-min source video (mp4, ~110 MB) — required
# for re-running the live SRT pipeline on the new box.
SRC_MP4="/tmp/v2v_compare/slice_5min.mp4"
if [[ -f "$SRC_MP4" ]]; then
    SRC_TGZ="/tmp/source_slice_5min.tgz"
    tar czf "$SRC_TGZ" -C /tmp v2v_compare/slice_5min.mp4
    ls -lh "$SRC_TGZ"
    echo "  → transfer this along with $OUT"
fi

if [[ $# -ge 1 ]]; then
    DEST="$1"
    echo "=== transferring to $DEST ==="
    scp "$OUT" "$DEST"
    [[ -f "$SRC_TGZ" ]] && scp "$SRC_TGZ" "$DEST"
fi

echo
echo "=== on the destination box ==="
echo "  1. tar xzf ai_commentator_src.tgz -C ~/commentary/"
echo "  2. mkdir -p /tmp/v2v_compare && tar xzf source_slice_5min.tgz -C /tmp/"
echo "  3. python -m venv .venv && .venv/bin/pip install openai numpy Pillow"
echo "     # for tracking work also: pip install ultralytics opencv-python"
echo "  4. Set .env with OPENAI_API_KEY / ELEVENLABS_API_KEY / GEMINI_API_KEY"
echo "  5. Test:  .venv/bin/python experiments/ai_commentator/detect_slowmo.py"
echo "     (should print 6 candidate slow-mo segments from the 5-min slice)"

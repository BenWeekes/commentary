#!/bin/bash
# v5 live test: R12 (roster attribution) + R13 (camera ban) + R7 (FR localizer).
# Builds v5_10s and v5_6s. RUN_TAG=_v5 keeps outputs separate from v4 (comparison intact).
# Pages built WITHOUT feedback UI (preview) — we self-check before opening a review round.
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4

run_profile () {
  local DELAY="$1" PVER="$2" ART="$3"
  echo "[[ $PVER ]] live run delay=${DELAY}s ($(date -u +%H:%M:%S))"
  BLEND_DELAY_S="$DELAY" BLEND_MODE=eager RUN_TAG=_v5 "$VENV" run_blend_true_live.py
  echo "[[ $PVER ]] mux en/fr/pt ($(date -u +%H:%M:%S))"
  for L in en fr pt; do
    "$VENV" mux_with_crowd.py "$SRC" "ai_blend_live_${L}_eager${ART}_track.wav" "blend_eager_${L}${ART}_synced.mp4"
  done
  echo "[[ $PVER ]] build preview page (no feedback UI)"
  PAGE_LAYOUT=v4 PAGE_VERSION="$PVER" BLEND_ARTIFACT_SUFFIX="$ART" CLIP_ID=mainz_union_md33_76-81 \
    "$VENV" build_hybrid_page.py
  echo "[[ $PVER ]] SELF-CHECK fixtures:"
  "$VENV" - "$ART" <<'PY'
import json, sys, eval_snapshot as E
art = sys.argv[1]
b = [json.loads(l) for l in open(f'commentary_blend_live_eager{art}.jsonl') if l.strip()]
fx = E.run_fixtures(b, f'vis_detections_eager{art}.jsonl')
print(f"   R12(attribution)={fx.get('R12')} R13(camera)={fx.get('R13')} "
      f"R7(fr-calques)={fx.get('R7')} R11(variety)={fx.get('R11')} | lines={len(b)}")
PY
}

run_profile 10 v5_10s _v5
run_profile 6  v5_6s  _6s_v5
echo "ALL DONE $(date -u +%H:%M:%S)"

#!/bin/bash
# v5_6s_vt: 6s fast profile, vision/tracker-only (USE_STT=0), v5 rules (R12/R13/R7 + guard).
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4
ART=_6s_vt_v5

echo "[[ v5_6s_vt ]] live run 6s STT-off ($(date -u +%H:%M:%S))"
BLEND_DELAY_S=6 BLEND_MODE=eager USE_STT=0 RUN_TAG=_v5 "$VENV" run_blend_true_live.py
echo "[[ v5_6s_vt ]] mux ($(date -u +%H:%M:%S))"
for L in en fr pt; do
  "$VENV" mux_with_crowd.py "$SRC" "ai_blend_live_${L}_eager${ART}_track.wav" "blend_eager_${L}${ART}_synced.mp4"
done
echo "[[ v5_6s_vt ]] build review page (feedback v5)"
PAGE_LAYOUT=v4 PAGE_VERSION=v5_6s_vt FEEDBACK_VERSION=v5 BLEND_ARTIFACT_SUFFIX="$ART" \
  CLIP_ID=mainz_union_md33_76-81 "$VENV" build_hybrid_page.py
echo "[[ v5_6s_vt ]] SELF-CHECK:"
"$VENV" - "$ART" <<'PY'
import json, sys, eval_snapshot as E
art=sys.argv[1]
b=[json.loads(l) for l in open(f'commentary_blend_live_eager{art}.jsonl') if l.strip()]
fx=E.run_fixtures(b, f'vis_detections_eager{art}.jsonl')
print(f"   R12={fx['R12']} R13={fx['R13']} R7={fx['R7']} R11={fx['R11']} lines={len(b)}")
PY
echo "V5_6SVT DONE $(date -u +%H:%M:%S)"

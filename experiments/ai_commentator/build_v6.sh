#!/bin/bash
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4
run() { # DELAY USE_STT PVER ART
  local D="$1" STT="$2" PVER="$3" ART="$4"
  echo "[[ $PVER ]] live ($(date -u +%H:%M:%S))"
  BLEND_DELAY_S="$D" BLEND_MODE=eager USE_STT="$STT" RUN_TAG=_v6 "$VENV" run_blend_true_live.py
  for L in en fr pt; do "$VENV" mux_with_crowd.py "$SRC" "ai_blend_live_${L}_eager${ART}_track.wav" "blend_eager_${L}${ART}_synced.mp4"; done
  PAGE_LAYOUT=v4 PAGE_VERSION="$PVER" FEEDBACK_VERSION=v6 BLEND_ARTIFACT_SUFFIX="$ART" CLIP_ID=mainz_union_md33_76-81 "$VENV" build_hybrid_page.py
  "$VENV" - "$ART" <<'PY'
import json,sys,eval_snapshot as E
art=sys.argv[1]; b=[json.loads(l) for l in open(f'commentary_blend_live_eager{art}.jsonl') if l.strip()]
fx=E.run_fixtures(b,f'vis_detections_eager{art}.jsonl')
print(f"   SELF-CHECK {art}: R12={fx['R12']} R13={fx['R13']} R7={fx['R7']} R11={fx['R11']} lines={len(b)}")
PY
}
run 10 1 v6_10s    _v6
run 6  1 v6_6s     _6s_v6
run 6  0 v6_6s_vt  _6s_vt_v6
echo "V6 ALL DONE $(date -u +%H:%M:%S)"

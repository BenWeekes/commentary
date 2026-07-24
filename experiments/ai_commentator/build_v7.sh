#!/bin/bash
# v7: codex-review fixes (roster keying, commit-time drop decision, shared shift,
# prio rollback, silent-localizer fix, clock grounding, FR glossary) across all
# three profiles. Unlike earlier runners this one ENFORCES the release gate
# (codex finding #4): unit tests first, then per profile the run FAILS unless
# all AUTO fixtures pass AND survival >= 0.95 AND desync shifts == 0 AND the
# first line lands within 2s. Pages are built WITHOUT the feedback UI (preview)
# — the round is opened manually only after the results are inspected.
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4

echo "[gate] unit tests"
BLEND_DELAY_S=6 "$VENV" test_blend_units.py > /dev/null && echo "   unit tests PASS"

run() { # DELAY USE_STT PVER ART
  local D="$1" STT="$2" PVER="$3" ART="$4"
  echo "[[ $PVER ]] live ($(date -u +%H:%M:%S))"
  BLEND_DELAY_S="$D" BLEND_MODE=eager USE_STT="$STT" RUN_TAG=_v7 "$VENV" run_blend_true_live.py
  echo "[[ $PVER ]] GATE"
  "$VENV" - "$ART" <<'PY'
import json, sys, eval_snapshot as E
art = sys.argv[1]
b = sorted([json.loads(l) for l in open(f'commentary_blend_live_eager{art}.jsonl') if l.strip()],
           key=lambda x: x['video_time_s'])
rep = json.load(open(f'latency_report_eager{art}.json'))
fx = E.run_fixtures(b, f'vis_detections_eager{art}.jsonl')
surv = rep.get('survival_rate')
desync = sum(1 for x in b if x.get('lat', {}).get('audio_shift_s', 0) > 1.5)
first = b[0]['video_time_s'] if b else None
auto = {k: v for k, v in fx.items() if v not in ('manual', 'skip')}
fails = []
if any(v is not True for v in auto.values()):
    fails.append(f"fixtures {dict((k, v) for k, v in auto.items() if v is not True)}")
if surv is None or surv < 0.95:
    fails.append(f"survival {surv}")
if desync != 0:
    fails.append(f"desync_shifts {desync}")
if first is None or first > 2.0:
    fails.append(f"first_line {first}")
print(f"   survival={surv} desync={desync} first={first} lines={len(b)} "
      f"fixtures={'all green' if not fails or 'fixtures' not in str(fails) else 'FAIL'}")
if fails:
    print(f"   GATE REJECT: {fails}")
    sys.exit(1)
print("   GATE PASS")
PY
  for L in en fr pt; do
    "$VENV" mux_with_crowd.py "$SRC" "ai_blend_live_${L}_eager${ART}_track.wav" "blend_eager_${L}${ART}_synced.mp4"
  done
  PAGE_LAYOUT=v4 PAGE_VERSION="$PVER" BLEND_ARTIFACT_SUFFIX="$ART" CLIP_ID=mainz_union_md33_76-81 \
    "$VENV" build_hybrid_page.py
}

run 10 1 v7_10s    _v7
run 6  1 v7_6s     _6s_v7
run 6  0 v7_6s_vt  _6s_vt_v7
echo "V7 ALL DONE $(date -u +%H:%M:%S) — every profile passed the gate"

#!/bin/bash
# v7 GATED runner — implements the documented acceptance process (codex-2 finding #1):
#   unit tests -> per profile: 3 live runs -> snapshot each (incl. hallucination judge)
#   -> eval_snapshot.py compare baseline r1 r2 r3 (worst-of-3, fail-closed fixtures)
#   -> only an ACCEPT builds the pages. Any REJECT stops the build.
# Runs are stashed under runs/v7/<profile>/rN_* so the trio is auditable.
set -euo pipefail
cd /home/ubuntu/commentary/experiments/ai_commentator
VENV=/home/ubuntu/commentary/.venv/bin/python
SRC=/tmp/v2v_compare/slice_5min.mp4
N_RUNS="${N_RUNS:-3}"
if [ "$N_RUNS" -lt 3 ]; then
  echo "REFUSED: N_RUNS=$N_RUNS — this runner builds RELEASE pages and the acceptance gate is worst-of-N (N>=3)." >&2
  echo "For exploration runs use run_blend_true_live.py directly; do not weaken the gate." >&2
  exit 1
fi

echo "[gate] unit tests"
BLEND_DELAY_S=6 "$VENV" test_blend_units.py > /dev/null && echo "   unit tests PASS"

run_profile() { # DELAY USE_STT PVER ART BASELINE
  local D="$1" STT="$2" PVER="$3" ART="$4" BASE_SNAP="$5"
  local RD="runs/v7/$PVER"; mkdir -p "$RD"
  local SNAPS=()
  for i in $(seq 1 "$N_RUNS"); do
    echo "[[ $PVER ]] live run $i/$N_RUNS ($(date -u +%H:%M:%S))"
    BLEND_DELAY_S="$D" BLEND_MODE=eager USE_STT="$STT" RUN_TAG=_v7 "$VENV" run_blend_true_live.py
    cp "commentary_blend_live_eager${ART}.jsonl" "$RD/r${i}_commentary.jsonl"
    cp "latency_report_eager${ART}.json"        "$RD/r${i}_latency.json"
    cp "vis_detections_eager${ART}.jsonl"       "$RD/r${i}_detections.jsonl" 2>/dev/null || true
    echo "[[ $PVER ]] snapshot $i (with hallucination judge)"
    "$VENV" eval_snapshot.py snapshot "commentary_blend_live_eager${ART}.jsonl" > "$RD/r${i}_snap.json"
    SNAPS+=("$RD/r${i}_snap.json")
  done
  echo "[[ $PVER ]] GATE: compare baseline vs worst-of-$N_RUNS"
  "$VENV" eval_snapshot.py compare "$BASE_SNAP" "${SNAPS[@]}"
  echo "[[ $PVER ]] GATE ACCEPT — building page from run $N_RUNS"
  for L in en fr pt; do
    "$VENV" mux_with_crowd.py "$SRC" "ai_blend_live_${L}_eager${ART}_track.wav" "blend_eager_${L}${ART}_synced.mp4"
  done
  PAGE_LAYOUT=v4 PAGE_VERSION="$PVER" BLEND_ARTIFACT_SUFFIX="$ART" CLIP_ID=mainz_union_md33_76-81 \
    "$VENV" build_hybrid_page.py
}

# Baselines = the accepted-state snapshots. Fall back to the v6 shipped artifacts
# (snapshotted fresh with the CURRENT fixture suite, fail-closed) if none exist.
mkdir -p runs/v7
for spec in "10:1:v7_10s:_v6" "6:1:v7_6s:_6s_v6" "6:0:v7_6s_vt:_6s_vt_v6"; do
  IFS=: read -r _ _ PV OLD <<< "$spec"
  BS="runs/v7/baseline_${PV}.json"
  if [ ! -f "$BS" ]; then
    echo "[baseline] snapshotting v6 artifact for $PV"
    "$VENV" eval_snapshot.py snapshot "commentary_blend_live_eager${OLD}.jsonl" > "$BS"
  fi
done

run_profile 10 1 v7_10s    _v7       runs/v7/baseline_v7_10s.json
run_profile 6  1 v7_6s     _6s_v7    runs/v7/baseline_v7_6s.json
run_profile 6  0 v7_6s_vt  _6s_vt_v7 runs/v7/baseline_v7_6s_vt.json
echo "V7 ALL DONE $(date -u +%H:%M:%S) — every profile ACCEPTED on worst-of-$N_RUNS"

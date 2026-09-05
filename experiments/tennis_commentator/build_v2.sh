#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/home/ubuntu/commentary/.venv/bin/python"
cd "$BASE"
VERSION="$("$PY" -c "from tennis_common import VERSION; print(VERSION)")"
PREVIOUS="v1"

echo "[1/7] isolation and prior-feedback check"
"$PY" -c "from tennis_common import assert_football_idle; assert_football_idle()"
"$PY" check_feedback.py --round "$PREVIOUS"

echo "[2/7] deterministic units"
"$PY" -m unittest -v test_tennis_units.py

echo "[3/7] validate immutable v1 clip/STT/vision inputs"
"$PY" validate_inputs.py

for profile in 10s 6s; do
  echo "[4/7] ${profile}: three complete commentary attempts"
  for attempt in 1 2 3; do
    TENNIS_PROFILE="$profile" "$PY" run_commentary.py --attempt "$attempt"
  done

  echo "[5/7] ${profile}: configured tennis EN/FR/pt-BR voices"
  TENNIS_PROFILE="$profile" "$PY" render_tracks.py

  echo "[6/7] ${profile}: strict judges and fail-closed worst-of-three gate"
  for attempt in 1 2 3; do
    TENNIS_PROFILE="$profile" "$PY" judge.py --attempt "$attempt"
  done
  TENNIS_PROFILE="$profile" "$PY" eval_tennis.py

done

echo "[7/7] publish both six-column pages, then open the review round"
for profile in 10s 6s; do
  TENNIS_PROFILE="$profile" "$PY" build_review_page.py
done
"$PY" open_round.py --previous "$PREVIOUS"

echo "ready: https://sa-dev.agora.io/experiments/tennis_commentator/${VERSION}_10s/"
echo "ready: https://sa-dev.agora.io/experiments/tennis_commentator/${VERSION}_6s/"

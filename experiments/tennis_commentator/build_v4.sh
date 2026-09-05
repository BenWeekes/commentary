#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/home/ubuntu/commentary/.venv/bin/python"
cd "$BASE"
VERSION="$("$PY" -c "from tennis_common import VERSION; print(VERSION)")"
PROFILES="$("$PY" -c "from tennis_common import PROFILES; print(' '.join(PROFILES))")"

echo "[1/8] isolation and v3 disposition audit"
"$PY" -c "from tennis_common import assert_football_idle; assert_football_idle()"
"$PY" check_feedback.py --round v3

echo "[2/8] deterministic units"
"$PY" -m unittest -v test_tennis_units.py

echo "[3/8] validate immutable clip/STT/vision inputs"
"$PY" validate_inputs.py

echo "[4/8] build and cross-check the local fast scoreboard observer"
"$PY" fast_scoreboard.py

for profile in $PROFILES; do
  echo "[5/8] ${profile}: three complete commentary attempts"
  for attempt in 1 2 3; do
    TENNIS_PROFILE="$profile" "$PY" run_commentary.py --attempt "$attempt"
  done

  echo "[6/8] ${profile}: prewarm configured EN/FR/pt-BR phrase cache and render"
  TENNIS_PROFILE="$profile" "$PY" render_tracks.py

  echo "[7/8] ${profile}: strict judges and fail-closed worst-of-three gate"
  for attempt in 1 2 3; do
    TENNIS_PROFILE="$profile" "$PY" judge.py --attempt "$attempt"
  done
  TENNIS_PROFILE="$profile" "$PY" eval_tennis.py
done

echo "[8/8] stage both passing six-column review pages"
for profile in $PROFILES; do
  TENNIS_PROFILE="$profile" "$PY" build_review_page.py
done

for profile in $PROFILES; do
  echo "staged: https://sa-dev.agora.io/experiments/tennis_commentator/${VERSION}_${profile}/"
done
echo "review round is not open and Slack is not notified until manual inspection passes"

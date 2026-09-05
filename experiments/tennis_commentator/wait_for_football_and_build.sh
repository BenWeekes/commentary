#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/home/ubuntu/commentary/.venv/bin/python"
LOCK="/tmp/tennis-commentator-build.lock"
IDLE_REQUIRED_S=180
POLL_S=15
cd "$BASE"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another tennis build is already queued or running"
  exit 1
fi

idle_since=0
while true; do
  if "$PY" -c \
    "from tennis_common import football_processes; raise SystemExit(0 if football_processes() else 1)"
  then
    idle_since=0
    echo "football active; tennis remains queued"
  else
    now="$(date +%s)"
    if (( idle_since == 0 )); then
      idle_since="$now"
      echo "football idle; waiting for a stable idle window"
    elif (( now - idle_since >= IDLE_REQUIRED_S )); then
      break
    fi
  fi
  sleep "$POLL_S"
done

echo "football stably idle; starting isolated tennis build"
./build_v3.sh
echo "v3 staged; manual inspection is required before opening the round or notifying Slack"

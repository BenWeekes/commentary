#!/bin/bash
# After clips are cut: run a Model E trial per clip (sequential), then Slack the URLs.
set -e
cd /home/ubuntu/commentary/experiments/ai_commentator/eros_trial
until [ -f random_windows.json ] && grep -q "ALL DONE" clips.log; do sleep 15; done
echo "clips ready; starting trials"
set -a; . /home/ubuntu/commentary/.env; set +a
python3 - <<'PY'
import json
base=json.load(open('../eros_test/pkg.json'))
for i,w in enumerate(json.load(open('random_windows.json')),1):
    p=dict(base)
    p['kickoff_state']={'period':w['period'],'clock':w['clock'],'home_score':w['home'],'away_score':w['away']}
    p['note']=f"5-minute live slice starting at {w['clock']}, score {w['home']}-{w['away']}"
    json.dump(p, open(f'pkg_r{i}.json','w'), indent=1)
PY
URLS=""
for i in 1 2 3 4 5; do
  N=$(python3 -c "import json;print(json.load(open('random_windows.json'))[$i-1]['name'])")
  echo "=== trial r$i ($N) ==="
  /home/ubuntu/commentary/.venv/bin/python trial.py --id "r$i" \
    --clip "/var/www/html/experiments/ai_commentator/md33_clips/$N" --pkg "pkg_r$i.json" || { echo "trial r$i FAILED"; continue; }
  URLS="$URLS\n• r$i (kickoff $(python3 -c "import json;w=json.load(open('random_windows.json'))[$i-1];print(w['clock'],str(w['home'])+'-'+str(w['away']))")): https://sa-dev.agora.io/experiments/ai_commentator/eros_trialr$i/"
  sleep 10
done
curl -s -X POST $SLACK_WEBHOOK \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"*AI Football commentator — Model E trials r1–r5 ready for review*\nFive random 5-min windows from the full Mainz–Union broadcast, Model E commentary voiced with our ElevenLabs EN commentator, synced to the moments described. Each page: video + pre-match data sent + line-by-line comments.$URLS\"}"
echo; echo "ALL TRIALS DONE"

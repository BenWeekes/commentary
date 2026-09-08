#!/bin/bash
set -e
cd /home/ubuntu/commentary/experiments/ai_commentator/eros_trial
# 1) cut 86:00-91:00 (file 8638s, 300s)
nice -n 15 ffmpeg -y -v error -threads 2 -ss 8638 -i /home/ubuntu/commentary/clips/md33_full/soccer_germany_bundesliga_8521005_3064k.mp4 \
  -t 300 -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 128k /home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_86_91.mp4
echo "clip cut"
# 2) package with this window's kickoff state
python3 - <<'PY'
import json
pkg=json.load(open('../eros_test/pkg.json'))
pkg['kickoff_state']={'period':'2','clock':'86:00','home_score':1,'away_score':1}
pkg['note']='5-minute live slice starting at 86:00, score 1-1 (window contains two goals)'
json.dump(pkg, open('pkg2.json','w'), indent=1)
PY
echo "pkg ready"
# 3) full trial: capture (WS, 6s deadline) -> voice -> page
set -a; . /home/ubuntu/commentary/.env; set +a
/home/ubuntu/commentary/.venv/bin/python trial.py --id 2 --clip /home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_86_91.mp4 --pkg pkg2.json

#!/bin/bash
set -e
cd /home/ubuntu/commentary/experiments/ai_commentator/eros_trial
set -a; . /home/ubuntu/commentary/.env; set +a
V=/home/ubuntu/commentary/.venv/bin/python
# re-voice EN for L1, r3, r4 (cached TTS -> just placement+mux)
$V trial.py --id L1 --skip-eros --clip /var/www/html/experiments/ai_commentator/md33_clips/r5_clock78m03.mp4 --pkg pkg_r5.json --langs en,fr,pt-BR,zh-CN
for i in 3 4; do
  N=$($V -c "import json;print(json.load(open('random_windows.json'))[$i-1]['name'])")
  $V trial.py --id r$i --skip-eros --clip /var/www/html/experiments/ai_commentator/md33_clips/$N --pkg pkg_r$i.json --langs en,zh-CN
done
# LS1: re-assemble (audio placement + signer re-render)
$V assemble_live_sign.py LS1 /var/www/html/experiments/ai_commentator/md33_clips/r5_clock78m03.mp4
python3 build_trial_page2.py LS1 work_LS1 pkg_r5.json /var/www/html/experiments/ai_commentator/modelE_trialLS1
echo REFIT-DONE

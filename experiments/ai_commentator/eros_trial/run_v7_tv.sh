#!/bin/bash
# v7: two voices (commentator + guest analyst). Vendor guidance: create matches >=15 min
# before streaming (background research needs the lead time) and include the match date.
set -e
cd /home/ubuntu/commentary/experiments/ai_commentator/eros_trial
set -a; . /home/ubuntu/commentary/.env; set +a
V=/home/ubuntu/commentary/.venv/bin/python
LANGS=en,fr,pt-BR,es,tr,zh-CN
# --- 1) pre-create all seven matches now ---
$V - <<'PY'
import json, os, time, urllib.request
ENV=os.environ; T=int(time.time()); mids={}
wins=['std']+[f'r{i}' for i in range(1,7)]
for w in wins:
    pkg=json.load(open('pkg_std_v2.json' if w=='std' else f'pkg_v2_{w}.json'))
    mid=f"trial{w}_7_tv-{T}"
    body={'match_id':mid,'match_package':pkg,'output':{'mode':'subtitle',
          'languages':['en','fr','pt-BR','es','tr','zh-CN'],'deadline_ms':6000,'commentary_profile':'tv'}}
    req=urllib.request.Request(ENV['EROS_API_BASE']+'/v1/matches',data=json.dumps(body).encode(),
        headers={'Authorization':f"Bearer {ENV['EROS_MATCH_TOKEN']}",'Content-Type':'application/json'})
    r=json.loads(urllib.request.urlopen(req,timeout=30).read())
    mids[w]=mid; print('created',mid,r.get('status'),flush=True)
json.dump({'created_at':T,'mids':mids},open('v7_mids.json','w'),indent=1)
PY
# --- 2) wait out the 15-minute research lead from creation ---
C=$($V -c "import json;print(json.load(open('v7_mids.json'))['created_at'])")
while [ $(( $(date +%s) - C )) -lt 960 ]; do sleep 30; done
echo "lead time satisfied ($(( $(date +%s) - C ))s); streaming"
# --- 3) stream each window against its pre-created match ---
URLS=""
run(){ # win id clip pkg label
  MID=$($V -c "import json;print(json.load(open('v7_mids.json'))['mids']['$1'])")
  $V trial.py --id "$2" --mid "$MID" --clip "$3" --pkg "$4" --profile tv --langs $LANGS \
    && URLS="$URLS\n• $5: https://sa-dev.agora.io/experiments/ai_commentator/modelE_trial$2/" \
    || echo "trial $2 FAILED"
  sleep 8
}
run std std_7_tv /home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_5min.mp4 pkg_std_v2.json "std (76:50, 1-1)"
for i in 1 2 3 4 5 6; do
  N=$($V -c "import json;print([w['name'] for w in json.load(open('random_windows.json')) if w['name'].startswith('r$i')][0])")
  W=$($V -c "import json;w=[x for x in json.load(open('random_windows.json')) if x['name'].startswith('r$i')][0];print(w['clock'],str(w['home'])+'-'+str(w['away']))")
  run "r$i" "r${i}_7_tv" "/var/www/html/experiments/ai_commentator/md33_clips/$N" "pkg_v2_r$i.json" "r$i ($W)"
done
curl -s -X POST "$SLACK_WEBHOOK" -H 'Content-Type: application/json' \
  -d "{\"text\":\"*AI Football commentator — Model E v7 TWO-VOICE trials ready for review*\nAll seven windows, TV style, now voiced as a double act: the commentator plus a guest analyst (different voice) for the between-action talk — speaker shown on every row. Per the vendor's guidance, matches were created 15+ minutes before streaming and include the match date, so the background talk should start from the first minute. Their delivery hints (intensity, pace) now shape each line's voice too.$URLS\"}"
echo; echo "V7TV BATCH DONE"

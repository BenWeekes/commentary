"""Post the window's two real official events to Eros (needs EROS_EVENT_TOKEN).
Times are clip PTS ms. Sources: gold STT + Alex-corrected facts:
  ~188.1s  yellow card, Kohn (Union Berlin)
  ~202.4s  Mainz double sub: Sieb + Weiper on (Tietz, Becker off per corrected review)
Run DURING the live publish, shortly after each moment passes."""
import json, os, time, urllib.request
TOK=os.environ['EROS_EVENT_TOKEN']; MID=os.environ['EROS_MATCH_ID']
EPOCH=json.load(open('arm.json'))['session']['stream_epoch']
sr=json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
num={}
for c in sr['lineups']['lineups']['competitors']:
    for p in c['players']:
        num[p['name'].split(',')[0].strip()]=(c['abbreviation'], p['jersey_number'])
def post(eid, pts, etype, player, team=None):
    t,n=num.get(player,(team,None))
    body={'match_id':MID,'event_id':eid,'revision':1,'stream_epoch':EPOCH,
          'source_pts_ms':pts,'received_pts_ms':pts+500,'period':'2',
          'event_type':etype,'team_id':t,'player_id':f"{t}:{n}" if n else None,
          'player_name':player,'status':'confirmed','authority':'official'}
    req=urllib.request.Request('https://live.nextmoment.ai/v1/events',
        data=json.dumps(body).encode(),
        headers={'Authorization':f'Bearer {TOK}','Content-Type':'application/json'})
    print(eid, urllib.request.urlopen(req, timeout=20).status)
EVENTS=[('test-card-kohn',188100,'CARD_YELLOW','Kohn'),
        ('test-sub-sieb',202400,'SUBSTITUTION','Sieb'),
        ('test-sub-weiper',202400,'SUBSTITUTION','Weiper')]
t0=time.time()
for eid,pts,etype,player in EVENTS:
    wait=pts/1000+2-(time.time()-t0)
    if wait>0: time.sleep(wait)
    post(eid,pts,etype,player)

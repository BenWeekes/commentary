"""Eros (nextmoment.ai) subtitle-mode test on the common 5-min Mainz-Union clip.

Usage:
  EROS_CONTROL_TOKEN=... EROS_STREAM_TOKEN=... python3 run_test.py [--match-id ID]
Phases (resumable-ish): create -> arm -> publish (ffmpeg -re) while polling en+zh-CN
-> save subtitles jsonl -> stats. Webpage built separately by build_page.py.
"""
import json, os, subprocess, sys, threading, time, urllib.request, pathlib
BASE=ENV.get('EROS_API_BASE','')
CTRL=os.environ.get('EROS_MATCH_TOKEN') or os.environ.get('EROS_CONTROL_TOKEN'); READ=os.environ.get('EROS_STREAM_TOKEN')
assert CTRL and READ, "set EROS_CONTROL_TOKEN and EROS_STREAM_TOKEN"
CLIP='/home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_5min.mp4'
MID=(sys.argv[sys.argv.index('--match-id')+1] if '--match-id' in sys.argv
     else f"m05-fcu-md33-test-{int(time.time())}")
HERE=pathlib.Path(__file__).parent

def api(path, tok, body=None, method=None):
    req=urllib.request.Request(BASE+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':f'Bearer {tok}','Content-Type':'application/json'},
        method=method or ('POST' if body is not None else 'GET'))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')

# --- match_package from our Sportradar cache: squads, numbers, kits, referee ---
sr=json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
L=sr['lineups']; comps=L['lineups']['competitors']
def team(c):
    return {'team_id':c['abbreviation'],'name':c['name'],'qualifier':c['qualifier'],
            'formation':(c.get('formation') or {}).get('type'),
            'kit':{k:v for k,v in (c.get('jersey') or {}).items() if k in ('base','sleeve','number')},
            'players':[{'player_id':f"{c['abbreviation']}:{p['jersey_number']}",
                        'name':p['name'],'number':p['jersey_number'],
                        'position':p.get('position') or p.get('type'),
                        'starter':bool(p.get('starter'))} for p in c['players']]}
pkg={'competition':'Bundesliga','venue':'Mewa Arena, Mainz',
     'kickoff_state':{'period':'2','clock':'76:50','home_score':1,'away_score':1},
     'referee':'Exner, Florian',
     'home_team':team(comps[0]),'away_team':team(comps[1]),
     'note':'5-minute live slice starting at 76:50, score 1-1'}

print('1) create', MID)
print(json.dumps(api('/v1/matches', CTRL, {'match_id':MID,'match_package':pkg,
    'output':{'mode':'subtitle','languages':['en','zh-CN'],'deadline_ms':8000}}))[:300])
print('2) arm')
arm=api(f'/v1/matches/{MID}/arm', CTRL, {'buffer_ms':8000})
json.dump(arm, open(HERE/'arm.json','w'), indent=1)
ffurl=arm['ingest'].get('ffmpeg_url') or arm['ingest']['url']
print('   epoch', arm.get('session',{}).get('stream_epoch'), '| ingest ok')

subs={'en':[], 'zh-CN':[]}; done=False
def poll(lang):
    cur=0
    while not done or cur==0:
        try:
            r=api(f'/v1/matches/{MID}/subtitles?after_sequence={cur}&language={lang}', READ)
            for s in r.get('subtitles', []):
                subs[lang].append(s|{'recv_unix_ms':int(time.time()*1000)})
                cur=max(cur, s['sequence'])
                if lang=='en': print(f"  [{s['source_pts_ms']/1000:6.1f}s p{s['priority']} lat={s.get('latency_ms')}ms] {s['text'][:70]}", flush=True)
        except Exception as e: print('poll',lang,str(e)[:80], flush=True)
        time.sleep(2)
threads=[threading.Thread(target=poll,args=(l,),daemon=True) for l in subs]
print('3) publish (real-time, ~300s) + poll')
for t in threads: t.start()
r=subprocess.run(['ffmpeg','-re','-i',CLIP,'-map','0:v:0','-map','0:a?','-c','copy','-f','mpegts',ffurl],
                 capture_output=True,text=True)
print('   ffmpeg rc', r.returncode, r.stderr[-200:] if r.returncode else '')
time.sleep(20)   # let the tail drain
done=True; time.sleep(3)
try: api(f'/v1/matches/{MID}/end', CTRL, {})
except Exception as e: print('end:', str(e)[:80])
for l,v in subs.items():
    (HERE/f"subs_{l.replace('-','_')}.jsonl").write_text('\n'.join(json.dumps(s) for s in v))
    lat=sorted(s.get('latency_ms',0) for s in v)
    print(f"{l}: {len(v)} lines", f"lat p50={lat[len(lat)//2]} p95={lat[int(len(lat)*.95)]}ms" if lat else '')

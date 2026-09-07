#!/usr/bin/env python3
"""LIVE sign-language trial: SRT to Model E; as EN lines arrive over WS, TTS them AND
generate Signapse ASL clips in real time (3 workers, chunked <=8 words). Every stage is
wall-clock stamped; the recording composites the signer at its TRUE ready time relative
to a 7s broadcast delay. Usage: live_sign_trial.py <trial_id> <clip> <pkg>"""
import json, os, re, subprocess, sys, threading, time, urllib.request, pathlib, queue
TID, CLIP, PKG = sys.argv[1:4]
AIC=pathlib.Path('/home/ubuntu/commentary/experiments/ai_commentator')
HERE=AIC/'eros_trial'; WORK=HERE/f'work_{TID}'; WORK.mkdir(exist_ok=True)
SIGNS=WORK/'signs'; SIGNS.mkdir(exist_ok=True); TTS=WORK/'tts_en'; TTS.mkdir(exist_ok=True)
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
BASE=ENV['EROS_API_BASE']; SR=16000; DELAY=7.0
def api(p,tok,body=None):
    req=urllib.request.Request(BASE+p, data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':f'Bearer {tok}','Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req,timeout=30).read() or b'{}')
def chunks(txt):
    parts=re.split(r'(?<=[.!?])\s+', txt.replace('—',','))
    res=[]
    for p in parts:
        ws=p.split()
        while len(ws)>8:
            idxs=[k for k,w in enumerate(ws) if w.endswith(',') and 2<k<len(ws)-2]
            cut=min(idxs,key=lambda k:abs(k-len(ws)//2))+1 if idxs else 8
            res.append(' '.join(ws[:cut])); ws=ws[cut:]
        if ws: res.append(' '.join(ws))
    return [r.strip(' ,') for r in res if r.strip(' ,')]
def signapse(txt,f,tries=4):
    body=json.dumps({"content":{"type":"text","data":txt},
        "output":{"format":"mp4","delivery":{"method":"download",
            "config":{"digitalSigner":"JAY","language":"ASL","backgroundColor":"#00FF00"}}},
        "context":{"application":"media"}}).encode()
    for a in range(tries):
        try:
            req=urllib.request.Request("https://ai.api.production.signapsesolutions.com/v2/generate",
                data=body,headers={"X-API-KEY":ENV['SIGNAPSE_API_KEY'],"Content-Type":"application/json"},method="POST")
            data=urllib.request.urlopen(req,timeout=90).read()
            if data[4:8]==b'ftyp': f.write_bytes(data); return True
        except Exception as e: print('signapse',str(e)[:40],flush=True); time.sleep(3)
    return False
# ---- create + arm
MID=f"{TID}-{int(time.time())}"
pkg=json.load(open(PKG))
api('/v1/matches',ENV['EROS_MATCH_TOKEN'],{'match_id':MID,'match_package':pkg,
    'output':{'mode':'subtitle','languages':['en'],'deadline_ms':6000}})
arm=api(f'/v1/matches/{MID}/arm',ENV['EROS_MATCH_TOKEN'],{'buffer_ms':8000})
print('armed',MID,flush=True)
lines=[]; t0=None; done=False
sign_q=queue.Queue(); events={}
def tts_line(i,text):
    f=TTS/f"{i:02d}.pcm"
    body=json.dumps({"text":text,"model_id":"eleven_flash_v2_5",
        "voice_settings":{"stability":0.5,"similarity_boost":0.8}}).encode()
    req=urllib.request.Request("https://api.elevenlabs.io/v1/text-to-speech/gU0LNdkMOQCOrPrwtbee?output_format=pcm_16000",
        data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
    f.write_bytes(urllib.request.urlopen(req,timeout=30).read())
    events[i]['tts_ready']=round(time.time()-t0,2)
def sign_worker():
    while True:
        item=sign_q.get()
        if item is None: return
        i,text=item
        events[i]['sign_start']=round(time.time()-t0,2)
        cs=chunks(text); fs=[]
        ok=True
        for k,c in enumerate(cs):
            f=SIGNS/f"tmp_{i:02d}_{k}.mp4"
            if not signapse(c,f): ok=False; break
            fs.append(f)
        if ok and fs:
            if len(fs)==1: fs[0].rename(SIGNS/f"{i:02d}.mp4")
            else:
                (SIGNS/f"cat_{i}.txt").write_text("".join(f"file '{x.name}'\n" for x in fs))
                subprocess.run(['ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{i}.txt",
                                '-c','copy',f"{i:02d}.mp4"],cwd=SIGNS,capture_output=True)
            events[i]['sign_ready']=round(time.time()-t0,2)
            print(f"  sign {i} ready at +{events[i]['sign_ready']}s (line pts {events[i]['pts']}s)",flush=True)
        else: events[i]['sign_failed']=True
workers=[threading.Thread(target=sign_worker,daemon=True) for _ in range(3)]
def ws_read():
    import asyncio, websockets
    async def go():
        cur=0
        while not done:
            try:
                async with websockets.connect(
                    f"{BASE.replace('https','wss')}/v1/matches/{MID}/subtitles/stream?after_sequence={cur}&language=en",
                    additional_headers={'Authorization':f"Bearer {ENV['EROS_STREAM_TOKEN']}"}) as w:
                    async for m in w:
                        l=json.loads(m); i=len(lines); lines.append(l); cur=max(cur,l['sequence'])
                        events[i]={'pts':round(l['source_pts_ms']/1000,2),'arr':round(time.time()-t0,2),'text':l['text']}
                        threading.Thread(target=tts_line,args=(i,l['text']),daemon=True).start()
                        if sign_q.qsize()>=4:
                            try: j,_=sign_q.get_nowait(); events[j]['sign_dropped']=True; print(f"  sign {j} DROPPED (backlog)",flush=True)
                            except queue.Empty: pass
                        sign_q.put((i,l['text']))
            except Exception as e:
                if not done: print('ws',str(e)[:50],flush=True); await asyncio.sleep(2)
    asyncio.run(go())
t0=time.time()
for w in workers: w.start()
threading.Thread(target=ws_read,daemon=True).start()
print('publishing live...',flush=True)
subprocess.run(['ffmpeg','-re','-i',CLIP,'-map','0:v:0','-map','0:a?','-c','copy','-f','mpegts',
                arm['ingest']['ffmpeg_url']],capture_output=True)
time.sleep(15)
# let sign backlog drain (up to 3 min)
t_end=time.time()+180
while time.time()<t_end and sign_q.qsize()>0: time.sleep(5)
time.sleep(10); done=True
for _ in workers: sign_q.put(None)
try: api(f'/v1/matches/{MID}/end',ENV['EROS_MATCH_TOKEN'],{})
except Exception: pass
json.dump(events,open(WORK/'live_events.json','w'),indent=1)
(WORK/'subs_en.jsonl').write_text('\n'.join(json.dumps(l) for l in lines))
signed=[i for i in events if 'sign_ready' in events[i]]
lags=[events[i]['sign_ready']-events[i]['pts'] for i in signed]
print(f"lines {len(lines)} | signed {len(signed)} | dropped {sum(1 for i in events if events[i].get('sign_dropped'))}",flush=True)
if lags: print(f"signer lag behind moment: p50 {sorted(lags)[len(lags)//2]:.1f}s max {max(lags):.1f}s",flush=True)
print('LIVE PHASE DONE',flush=True)

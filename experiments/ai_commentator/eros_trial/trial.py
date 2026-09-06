#!/usr/bin/env python3
"""One-command Eros trial on any clip: capture -> voice -> review page.

Usage:
  python3 trial.py --id 2 --clip /path/clip.mp4 --pkg /path/match_package.json
  (tokens read from /home/ubuntu/commentary/.env; add --skip-eros to rebuild
   page/voice from an existing capture in this trial's work dir)

Steps: create+arm Eros match -> ffmpeg -re publish -> poll en+zh-CN ->
ElevenLabs-voice the EN lines at source_pts_ms (overlap-shifted) -> mux with
crowd bed -> review page + video at /experiments/ai_commentator/eros_trial<id>/.
"""
import argparse, json, os, pathlib, subprocess, sys, threading, time, urllib.request
AIC=pathlib.Path('/home/ubuntu/commentary/experiments/ai_commentator')
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
BASE=ENV.get('EROS_API_BASE',''); SR=16000
ap=argparse.ArgumentParser(); ap.add_argument('--id',required=True); ap.add_argument('--clip',required=True)
ap.add_argument('--pkg',required=True); ap.add_argument('--skip-eros',action='store_true')
ap.add_argument('--langs',default='en,zh-CN')
ap.add_argument('--deadline',type=int,default=6000)   # 7s-delay budget: text<=6s + TTS ~0.7s + margin
a=ap.parse_args()
WORK=AIC/f'eros_trial/work_{a.id}'; WORK.mkdir(parents=True, exist_ok=True)
WWW=pathlib.Path(f'/var/www/html/experiments/ai_commentator/eros_trial{a.id}'); WWW.mkdir(exist_ok=True)
dur=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',a.clip],capture_output=True,text=True).stdout.strip())
def api(path, tok, body=None):
    req=urllib.request.Request(BASE+path, data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':f'Bearer {tok}','Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read() or b'{}')
langs=a.langs.split(',')
if not a.skip_eros:
    MID=f"trial{a.id}-{int(time.time())}"
    pkg=json.load(open(a.pkg))
    api('/v1/matches', ENV['EROS_MATCH_TOKEN'], {'match_id':MID,'match_package':pkg,
        'output':{'mode':'subtitle','languages':langs,'deadline_ms':a.deadline}})
    arm=api(f'/v1/matches/{MID}/arm', ENV['EROS_MATCH_TOKEN'], {'buffer_ms':8000})
    (WORK/'arm.json').write_text(json.dumps(arm)); print('armed', MID, flush=True)
    subs={l:[] for l in langs}; done=False
    def ws_read(lang):   # WebSocket: lines arrive ~170ms after emit (vs ~1s polling)
        import asyncio, websockets
        async def go():
            cur=0
            while not done:
                try:
                    async with websockets.connect(
                        f"{BASE.replace(chr(104)+chr(116)+chr(116)+chr(112)+chr(115),chr(119)+chr(115)+chr(115))}/v1/matches/{MID}/subtitles/stream?after_sequence={cur}&language={lang}",
                        additional_headers={'Authorization':f"Bearer {ENV['EROS_STREAM_TOKEN']}"}) as w:
                        async for m in w:
                            l=json.loads(m); l['recv_unix_ms']=int(time.time()*1000)
                            subs[lang].append(l); cur=max(cur,l['sequence'])
                except Exception as e:
                    if not done: print('ws',lang,str(e)[:60],flush=True); await asyncio.sleep(2)
        asyncio.run(go())
    th=[threading.Thread(target=ws_read,args=(l,),daemon=True) for l in langs]
    for t in th: t.start()
    r=subprocess.run(['ffmpeg','-re','-i',a.clip,'-map','0:v:0','-map','0:a?','-c','copy','-f','mpegts',
                      arm['ingest']['ffmpeg_url']],capture_output=True,text=True)
    print('publish rc',r.returncode, r.stderr[-150:] if r.returncode else '', flush=True)
    time.sleep(20); done=True; time.sleep(3)
    try: api(f'/v1/matches/{MID}/end', ENV['EROS_MATCH_TOKEN'], {})
    except Exception: pass
    for l in langs:
        (WORK/f"subs_{l.replace('-','_')}.jsonl").write_text('\n'.join(json.dumps(s) for s in subs[l]))
        print(l, len(subs[l]), 'lines', flush=True)
# voice EN at pts
lines=[json.loads(x) for x in open(WORK/'subs_en.jsonl')]; lines.sort(key=lambda l:l['source_pts_ms'])
td=WORK/'tts'; td.mkdir(exist_ok=True)
track=bytearray(int(dur)*SR*2); prev=0.0; placed=[]
for i,l in enumerate(lines):
    f=td/f"{i:02d}.pcm"
    if not (f.exists() and f.stat().st_size>4000):
        body=json.dumps({"text":l['text'],"model_id":"eleven_flash_v2_5",
            "voice_settings":{"stability":0.5,"similarity_boost":0.8}}).encode()
        req=urllib.request.Request("https://api.elevenlabs.io/v1/text-to-speech/gU0LNdkMOQCOrPrwtbee?output_format=pcm_16000",
            data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
        f.write_bytes(urllib.request.urlopen(req,timeout=60).read())
    d=f.stat().st_size/2/SR; t=max(l['source_pts_ms']/1000, prev+0.2)
    if t+d>dur: break
    p=int(t*SR)*2; pcm=f.read_bytes(); track[p:p+len(pcm)]=pcm
    prev=t+d; placed.append({'i':i,'t':round(t,2),'pts':l['source_pts_ms']/1000,'dur':round(d,2)})
(WORK/'track.pcm').write_bytes(bytes(track))
json.dump(placed, open(WORK/'placement.json','w'))
subprocess.run(['ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1','-i',str(WORK/'track.pcm'),str(WORK/'track.wav')],check=True)
subprocess.run(['python3',str(AIC/'mux_with_crowd.py'),a.clip,str(WORK/'track.wav'),str(WWW/'eros_en.mp4')],check=True)
subprocess.run(['python3',str(AIC/'eros_trial/build_trial_page.py'),a.id,str(WORK/'subs_en.jsonl'),
                str(WORK/'placement.json'),a.pkg,str(WWW)],check=True)
print(f"READY: https://sa-dev.agora.io/experiments/ai_commentator/eros_trial{a.id}/")

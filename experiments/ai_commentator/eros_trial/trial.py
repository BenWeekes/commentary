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
WWW=pathlib.Path(f'/var/www/html/experiments/ai_commentator/modelE_trial{a.id}'); WWW.mkdir(exist_ok=True)
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
# ---- PRECISE VOICING ENGINE ----------------------------------------------
# Every utterance starts at its source_pts sample-exactly (PCM placement, <<50ms).
# Preemption: a HIGHER-priority line (lower number) cuts the running utterance
# (60ms fade, status=cut); a lower/equal-priority line arriving mid-utterance is
# dropped (status=dropped). Full disclosure per line in placement_<lang>.json.
VOICE={'en':'gU0LNdkMOQCOrPrwtbee','fr':'LcKoSBj8CeBInl4bQHtq','pt-BR':'HR2TRGmi4QbMsO5omv7l'}
DEFAULT_VOICE='gU0LNdkMOQCOrPrwtbee'
def tts_fetch(text, voice, f):
    if f.exists() and f.stat().st_size>4000: return
    body=json.dumps({"text":text,"model_id":"eleven_flash_v2_5",
        "voice_settings":{"stability":0.5,"similarity_boost":0.8}}).encode()
    req=urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
    f.write_bytes(urllib.request.urlopen(req,timeout=60).read())
for lang in langs:
    sf=WORK/f"subs_{lang.replace('-','_')}.jsonl"
    if not sf.exists(): continue
    lines=[json.loads(x) for x in open(sf)]; lines.sort(key=lambda l:l['source_pts_ms'])
    td=WORK/f"tts_{lang.replace('-','_')}"; td.mkdir(exist_ok=True)
    voice=VOICE.get(lang, DEFAULT_VOICE)
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda t: tts_fetch(t[1]['text'], voice, td/f"{t[0]:02d}.pcm"), enumerate(lines)))
    track=bytearray(int(dur)*SR*2)
    cur=None   # {'i','end_sample','prio'}
    report=[]
    for i,l in enumerate(lines):
        pts=l['source_pts_ms']/1000; prio=l.get('priority',3)
        f=td/f"{i:02d}.pcm"
        if not f.exists() or f.stat().st_size<4000:
            report.append({'i':i,'seq':l['sequence'],'pts':round(pts,2),'prio':prio,'status':'tts_failed'}); continue
        pcm=f.read_bytes(); dsamp=len(pcm)//2
        p0=int(round(pts*SR))
        if p0+16 >= int(dur)*SR:
            report.append({'i':i,'seq':l['sequence'],'pts':round(pts,2),'prio':prio,'status':'dropped','reason':'beyond clip'}); continue
        if cur and p0 < cur['end_sample']:
            if prio < cur['prio']:
                # cut the running utterance NOW with a 60ms fade
                fs=max(p0-int(0.06*SR), 0)
                for k in range(fs, p0):
                    idx=k*2
                    v=int.from_bytes(track[idx:idx+2],'little',signed=True)
                    g=1.0-(k-fs)/max(p0-fs,1)
                    track[idx:idx+2]=int(v*g).to_bytes(2,'little',signed=True)
                track[p0*2:cur['end_sample']*2]=b'\x00'*((cur['end_sample']-p0)*2)
                for r in report:
                    if r['i']==cur['i']:
                        r['status']='cut'; r['cut_at']=round(p0/SR,2); break
                cur=None
            else:
                report.append({'i':i,'seq':l['sequence'],'pts':round(pts,2),'prio':prio,'status':'dropped',
                               'reason':f"p{cur['prio']} line still speaking"}); continue
        ends=min(p0+dsamp, len(track)//2)
        track[p0*2:ends*2]=pcm[:(ends-p0)*2]
        cur={'i':i,'end_sample':ends,'prio':prio}
        report.append({'i':i,'seq':l['sequence'],'pts':round(pts,2),'prio':prio,'status':'played',
                       't':round(pts,2),'dur':round(dsamp/SR,2)})
    (WORK/f'track_{lang}.pcm').write_bytes(bytes(track))
    json.dump(report, open(WORK/f"placement_{lang.replace('-','_')}.json",'w'))
    if lang=='en':
        json.dump([r for r in report if r['status'] in ('played','cut')], open(WORK/'placement.json','w'))
    subprocess.run(['ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1',
        '-i',str(WORK/f'track_{lang}.pcm'),str(WORK/f'track_{lang}.wav')],check=True)
    subprocess.run(['python3',str(AIC/'mux_with_crowd.py'),a.clip,str(WORK/f'track_{lang}.wav'),
        str(WWW/f"modelE_{lang.replace('-','_')}.mp4")],check=True)
    st={r['status'] for r in report}
    print('voiced',lang,'played',sum(1 for r in report if r['status']=='played'),
          'cut',sum(1 for r in report if r['status']=='cut'),
          'dropped',sum(1 for r in report if r['status']=='dropped'),flush=True)
subprocess.run(['python3',str(AIC/'eros_trial/build_trial_page2.py'),a.id,str(WORK),a.pkg,str(WWW)],check=True)
print(f"READY: https://sa-dev.agora.io/experiments/ai_commentator/modelE_trial{a.id}/")

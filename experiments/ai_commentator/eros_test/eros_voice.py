"""Voice Eros EN lines (ElevenLabs, our EN voice) placed at source_pts_ms on the clip."""
import json, os, subprocess, urllib.request, pathlib
SR=16000
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
HERE=pathlib.Path(__file__).parent; td=HERE/'tts'; td.mkdir(exist_ok=True)
lines=[json.loads(x) for x in open(HERE/'subs_en.jsonl')]
lines.sort(key=lambda l:l['source_pts_ms'])
def tts(text,f):
    if f.exists() and f.stat().st_size>4000: return
    body=json.dumps({"text":text,"model_id":"eleven_flash_v2_5",
        "voice_settings":{"stability":0.5,"similarity_boost":0.8}}).encode()
    req=urllib.request.Request("https://api.elevenlabs.io/v1/text-to-speech/gU0LNdkMOQCOrPrwtbee?output_format=pcm_16000",
        data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
    f.write_bytes(urllib.request.urlopen(req,timeout=60).read())
track=bytearray(300*SR*2); prev_end=0.0; placed=[]
for i,l in enumerate(lines):
    f=td/f"{i:02d}.pcm"; tts(l['text'],f)
    dur=f.stat().st_size/2/SR
    t=max(l['source_pts_ms']/1000, prev_end+0.2)   # never overlap; never earlier than its pts
    if t+dur>300: break
    p=int(t*SR)*2; pcm=f.read_bytes(); track[p:p+len(pcm)]=pcm
    prev_end=t+dur; placed.append({'i':i,'t':round(t,2),'pts':l['source_pts_ms']/1000,'dur':round(dur,2)})
(HERE/'eros_en_track.pcm').write_bytes(bytes(track))
json.dump(placed, open(HERE/'placement.json','w'))
subprocess.run(['ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1','-i',str(HERE/'eros_en_track.pcm'),str(HERE/'eros_en_track.wav')],check=True)
r=subprocess.run(['python3','/home/ubuntu/commentary/experiments/ai_commentator/mux_with_crowd.py',
    '/home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_5min.mp4',
    str(HERE/'eros_en_track.wav'), str(HERE/'eros_en.mp4')],capture_output=True,text=True)
print('mux rc',r.returncode, r.stderr[-150:] if r.returncode else 'OK')
shift=[p for p in placed if p['t']-p['pts']>0.3]
print(f"placed {len(placed)}/{len(lines)} lines; shifted-for-overlap: {len(shift)} (max {max((p['t']-p['pts'] for p in shift),default=0):.1f}s)")

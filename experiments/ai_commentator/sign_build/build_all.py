"""ASL signer demo builder — resumable, all phases cached on disk.
Phases: 1 sign clips (chunked<=8w, concat)  2 TTS  3 timeline+cut  4 batched overlay  5 mux."""
import json, re, subprocess, urllib.request, pathlib, time, sys
SR=16000
HERE=pathlib.Path(__file__).parent
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
BASE='/var/www/html/experiments/ai_commentator/blend_v7_10s/eager_en.mp4'
IDLE='/home/ubuntu/commentary/experiments/ai_commentator/sign_build/assets/idle-jay-asl-green.mp4'
ATMOS='/home/ubuntu/commentary/clips/bmg_fch_demo_5min/atmosphere.wav'
lines=json.load(open('/home/ubuntu/commentary/experiments/ai_commentator/sign_build/assets/mainz-union-en.json'))
signs=HERE/'signs'; tdir=HERE/'tts'; signs.mkdir(exist_ok=True); tdir.mkdir(exist_ok=True)
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','10','ffmpeg','-threads','2']+a[1:]
    return subprocess.run(a,capture_output=True,text=True,**k)
def vdur(f): return float(sh('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(f)).stdout.strip())

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

def signapse(txt, f, tries=10):
    body=json.dumps({"content":{"type":"text","data":txt},
        "output":{"format":"mp4","delivery":{"method":"download",
            "config":{"digitalSigner":"JAY","language":"ASL","backgroundColor":"#00FF00"}}},
        "context":{"application":"media"}}).encode()
    for a in range(tries):
        try:
            req=urllib.request.Request("https://ai.api.production.signapsesolutions.com/v2/generate",
                data=body,headers={"X-API-KEY":ENV['SIGNAPSE_API_KEY'],"Content-Type":"application/json"},method="POST")
            data=urllib.request.urlopen(req,timeout=150).read()
            if data[4:8]==b'ftyp': f.write_bytes(data); return True
            raise RuntimeError(f"body {data[:30]!r}")
        except Exception as e:
            print(f.name,"attempt",a+1,str(e)[:50],flush=True); time.sleep(8)
    return False

# phase 1: sign clips
for i,l in enumerate(lines):
    out=signs/f"{i:02d}.mp4"
    if out.exists() and out.stat().st_size>10000: continue
    cs=chunks(l['text']); fs=[]
    for k,c in enumerate(cs):
        f=signs/f"tmp_{i:02d}_{k}.mp4"
        if not (f.exists() and f.stat().st_size>10000):
            if not signapse(c,f): sys.exit(f"sign {i} chunk {k} failed")
        fs.append(f)
    if len(fs)==1: fs[0].rename(out)
    else:
        (signs/f"cat_{i}.txt").write_text("".join(f"file '{f.name}'\n" for f in fs))
        r=sh('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{i}.txt",'-c','copy',f"{i:02d}.mp4",cwd=signs)
        if r.returncode: sys.exit(f"concat {i}: {r.stderr[:150]}")
    print("sign",i,"done",flush=True)
print("phase1 complete",flush=True)

# phase 2: TTS
for i,l in enumerate(lines):
    f=tdir/f"{i:02d}.pcm"
    if f.exists() and f.stat().st_size>4000: continue
    body=json.dumps({"text":l['text'],"model_id":"eleven_flash_v2_5",
        "voice_settings":{"stability":0.5,"similarity_boost":0.8}}).encode()
    req=urllib.request.Request("https://api.elevenlabs.io/v1/text-to-speech/gU0LNdkMOQCOrPrwtbee?output_format=pcm_16000",
        data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
    f.write_bytes(urllib.request.urlopen(req,timeout=60).read()); print("tts",i,flush=True)
print("phase2 complete",flush=True)

# phase 3: timeline + cut + audio
d=[(tdir/f"{i:02d}.pcm").stat().st_size/2/SR for i in range(len(lines))]
s=[vdur(signs/f"{i:02d}.mp4") for i in range(len(lines))]
PRE,POST=1.0,0.7
win=[]
for i,l in enumerate(lines):
    a=max(0,l['t']-PRE); b=min(300,l['t']+max(d[i],min(s[i],d[i]+4.0))+POST)
    if win and a<=win[-1][1]+1.2: win[-1][1]=max(win[-1][1],b)
    else: win.append([a,b])
off=[];acc=0
for a,b in win: off.append(acc-a); acc+=b-a
def remap(t):
    for (a,b),o in zip(win,off):
        if a-0.01<=t<=b+0.01: return t+o
nt=[remap(l['t']) for l in lines]
segend=[];a2=0
for a,b in win: a2+=b-a; segend.append(a2)
def se(t2):
    for e in segend:
        if t2<=e+0.01: return e
    return a2
disp=[]
for i in range(len(lines)):
    e=nt[i]+s[i]
    if i+1<len(lines): e=min(e,nt[i+1])
    disp.append((nt[i],min(e,se(nt[i]))))
print(f"phase3: {len(win)} segs, {acc:.1f}s",flush=True)
if not (HERE/'cut.mp4').exists():
    sel='+'.join(f"between(t,{a:.3f},{b:.3f})" for a,b in win)
    r=sh('ffmpeg','-y','-v','error','-i',BASE,'-vf',f"select='{sel}',setpts=N/25/TB",'-r','25','-an',
         '-c:v','libx264','-preset','veryfast','-crf','18',str(HERE/'cut.mp4'))
    if r.returncode: sys.exit("cut: "+r.stderr[-200:])
    if vdur(HERE/'cut.mp4') < 60: sys.exit("cut.mp4 invalid/truncated")
n=int(acc*SR); track=bytearray(n*2)
for i in range(len(lines)):
    pcm=(tdir/f"{i:02d}.pcm").read_bytes(); p=int(nt[i]*SR)*2
    end=min(len(track),p+len(pcm)); track[p:end]=pcm[:end-p]
(HERE/'en_only.pcm').write_bytes(bytes(track))
r=sh('ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1','-i',str(HERE/'en_only.pcm'),'-i',ATMOS,
    '-filter_complex',f'[1:a]atrim=0:{acc},aformat=channel_layouts=mono,volume=-16dB[bed];[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0[m]',
    '-map','[m]','-ar','44100',str(HERE/'audio_mix.wav'))
if r.returncode: sys.exit("audio: "+r.stderr[-200:])
print("phase3 complete",flush=True)

# phase 4: batched overlays
W=380; XOFF=22
KEY='crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green'
N=len(lines); BATCH=6; cur=str(HERE/'cut.mp4')
for b0 in range(0,N,BATCH):
    idx=list(range(b0,min(b0+BATCH,N))); last=b0+BATCH>=N
    inputs=['-i',cur]; nin=1; flt=[]; prev='0:v'
    if b0==0:
        active='+'.join(f"between(t,{a:.2f},{b:.2f})" for a,b in disp)
        inputs+=['-stream_loop','-1','-t',f"{acc:.1f}",'-i',IDLE]; nin=2
        flt.append(f"[1:v]{KEY},scale={W}:-2[idle]")
        flt.append(f"[0:v][idle]overlay=x=W-w+{XOFF}:y=H-h:shortest=1:enable='if({active},0,1)'[v0]")
        prev='v0'
    for i in idx:
        nI=nin; nin+=1
        inputs+=['-i',str(signs/f"{i:02d}.mp4")]
        flt.append(f"[{nI}:v]{KEY},scale={W}:-2,setpts=PTS-STARTPTS+{nt[i]:.3f}/TB[s{i}]")
        flt.append(f"[{prev}][s{i}]overlay=x=W-w+{XOFF}:y=H-h:enable='between(t,{disp[i][0]:.2f},{disp[i][1]:.2f})'[o{i}]")
        prev=f"o{i}"
    out=str(HERE/(f"stage_{b0}.mp4" if not last else "video_only.mp4"))
    (HERE/f"g_{b0}.txt").write_text(';'.join(flt))
    r=sh('ffmpeg','-y',*inputs,'-filter_complex_script',str(HERE/f"g_{b0}.txt"),'-map',f'[{prev}]',
        '-c:v','libx264','-preset','veryfast','-crf','20' if last else '15','-an','-t',f"{acc:.1f}",out)
    print(f"batch {b0} rc {r.returncode}",r.stderr[-200:] if r.returncode else '',flush=True)
    if r.returncode: sys.exit(1)
    cur=out

# phase 5: mux
r=sh('ffmpeg','-y','-v','error','-i',str(HERE/'video_only.mp4'),'-i',str(HERE/'audio_mix.wav'),
    '-map','0:v','-map','1:a','-c:v','copy','-c:a','aac','-b:a','128k','-t',f"{acc:.1f}",str(HERE/'mainz_union_asl_tight.mp4'))
print("mux rc",r.returncode, r.stderr[-150:] if r.returncode else "ALL DONE",flush=True)

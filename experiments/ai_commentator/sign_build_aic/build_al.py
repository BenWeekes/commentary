"""al.mp4 + ASL signer burn-in. Resumable. Original audio kept."""
import json, re, subprocess, urllib.request, pathlib, time, sys
HERE=pathlib.Path(__file__).parent
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
BASE=str(pathlib.Path.home()/'ai_commentary.mp4')
IDLE='/home/ubuntu/sign-video-client/public/signer-overlay/idle-jay-asl-green.mp4'
signs=HERE/'signs'; signs.mkdir(exist_ok=True)
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','15','ffmpeg','-threads','2']+a[1:]
    return subprocess.run(a,capture_output=True,text=True,**k)
def vdur(f): return float(sh('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(f)).stdout.strip())

# rebuild lines from tokens: drop <end>/<fin>/<endpoint>, sentence/gap split, merge tiny lines
toks=json.load(open('tokens.json'))
toks=[t for t in toks if t['text'] not in ('<end>','<fin>','<endpoint>')]
words=[]; buf=''; t0=None
for t in toks:
    txt=t['text']
    if txt.startswith(' ') and buf: words.append((buf,t0)); buf,t0='',None
    if buf=='': t0=t['start_ms']/1000.0
    buf += txt.strip() if buf=='' else txt
if buf: words.append((buf,t0))
lines=[]; cur=[]; start=None; last=0
for w,ts in words:
    if cur and ts-last>1.5:
        lines.append({'t':start,'text':' '.join(cur)}); cur=[]; start=None
    if start is None: start=ts
    cur.append(w); last=ts
    if re.search(r'[.!?]$',w) and len(cur)>=4:
        lines.append({'t':start,'text':' '.join(cur)}); cur=[]; start=None
if cur: lines.append({'t':start,'text':' '.join(cur)})
merged=[]
for l in lines:
    if merged and len(l['text'].split())<4 and l['t']-merged[-1]['t']<12:
        merged[-1]['text']+=' '+l['text']
    else: merged.append(l)
lines=merged
json.dump(lines,open(HERE/'lines_clean.json','w'),indent=1)
print(len(lines),"lines",flush=True)

def chunks(txt):
    parts=re.split(r'(?<=[.!?])\s+', txt.replace('—',', '))
    res=[]
    for p in parts:
        ws=p.split()
        while len(ws)>8:
            idxs=[k for k,w in enumerate(ws) if w.endswith(',') and 2<k<len(ws)-2]
            cut=min(idxs,key=lambda k:abs(k-len(ws)//2))+1 if idxs else 8
            res.append(' '.join(ws[:cut])); ws=ws[cut:]
        if ws: res.append(' '.join(ws))
    return [r.strip(' ,') for r in res if r.strip(' ,')]
def signapse(txt,f,tries=10):
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
for i,l in enumerate(lines):
    out=signs/f"{i:02d}.mp4"
    if out.exists() and out.stat().st_size>10000: continue
    fs=[]
    for k,c in enumerate(chunks(l['text'])):
        f=signs/f"tmp_{i:02d}_{k}.mp4"
        if not (f.exists() and f.stat().st_size>10000):
            if not signapse(c,f): sys.exit(f"sign {i} chunk {k} failed")
        fs.append(f)
    if len(fs)==1: fs[0].rename(out)
    else:
        (signs/f"cat_{i}.txt").write_text("".join(f"file '{f.name}'\n" for f in fs))
        r=sh('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{i}.txt",'-c','copy',f"{i:02d}.mp4",cwd=signs)
        if r.returncode: sys.exit(f"concat {i}: {r.stderr[:120]}")
    print("sign",i,"done",flush=True)
print("clips complete",flush=True)

dur=vdur(BASE); s=[vdur(signs/f"{i:02d}.mp4") for i in range(len(lines))]
nt=[min(l['t']+4.0, dur-2.0) for l in lines]   # realistic interpreter lag: sign trails audio ~4s
disp=[]
for i in range(len(lines)):
    e=nt[i]+s[i]
    if i+1<len(lines): e=min(e,nt[i+1])
    disp.append((nt[i],min(e,dur)))
W=190; XOFF=12   # small signer on 640x360
KEY='crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green'
N=len(lines); BATCH=6; cur=BASE
for b0 in range(0,N,BATCH):
    idx=list(range(b0,min(b0+BATCH,N))); last=b0+BATCH>=N
    inputs=['-i',cur]; nin=1; flt=[]; prev='0:v'
    if b0==0:
        active='+'.join(f"between(t,{a:.2f},{b:.2f})" for a,b in disp)
        inputs+=['-stream_loop','-1','-t',f"{dur:.1f}",'-i',IDLE]; nin=2
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
        '-c:v','libx264','-preset','veryfast','-crf','20' if last else '15','-an','-t',f"{dur:.2f}",out)
    print(f"batch {b0} rc {r.returncode}",r.stderr[-150:] if r.returncode else '',flush=True)
    if r.returncode: sys.exit(1)
    cur=out
r=sh('ffmpeg','-y','-v','error','-i',str(HERE/'video_only.mp4'),'-i',BASE,'-map','0:v','-map','1:a',
    '-c:v','copy','-c:a','copy','-t',f"{dur:.2f}",str(HERE/'ai_commentary_asl.mp4'))
print("mux rc",r.returncode, r.stderr[-120:] if r.returncode else "ALL DONE",flush=True)

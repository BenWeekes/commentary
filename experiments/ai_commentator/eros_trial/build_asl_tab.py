#!/usr/bin/env python3
"""ASL tab for a Model E trial: sign the EN lines, burn onto the EN-voiced video.
Signer lag per line = MEASURED Signapse generation time (the honest live delay).
Resumable; durable under work_<id>/signs/. Usage: build_asl_tab.py <trial_id>"""
import json, re, subprocess, sys, time, urllib.request, pathlib
TID=sys.argv[1]
HERE=pathlib.Path(__file__).parent; WORK=HERE/f'work_{TID}'; SIGNS=WORK/'signs'; SIGNS.mkdir(exist_ok=True)
WWW=pathlib.Path(f'/var/www/html/experiments/ai_commentator/modelE_trial{TID}')
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
IDLE='/home/ubuntu/commentary/experiments/ai_commentator/sign_build/assets/idle-jay-asl-green.mp4'
BASE=str(WWW/'modelE_en.mp4')
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','15','ffmpeg','-threads','2']+a[1:]
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
def signapse(txt,f,tries=10):
    body=json.dumps({"content":{"type":"text","data":txt},
        "output":{"format":"mp4","delivery":{"method":"download",
            "config":{"digitalSigner":"JAY","language":"ASL","backgroundColor":"#00FF00"}}},
        "context":{"application":"media"}}).encode()
    for a in range(tries):
        try:
            t0=time.time()
            req=urllib.request.Request("https://ai.api.production.signapsesolutions.com/v2/generate",
                data=body,headers={"X-API-KEY":ENV['SIGNAPSE_API_KEY'],"Content-Type":"application/json"},method="POST")
            data=urllib.request.urlopen(req,timeout=150).read()
            if data[4:8]==b'ftyp': f.write_bytes(data); return time.time()-t0
            raise RuntimeError(f"body {data[:30]!r}")
        except Exception as e:
            print(f.name,'attempt',a+1,str(e)[:50],flush=True); time.sleep(8)
    return None
lines=[json.loads(x) for x in open(WORK/'subs_en.jsonl')]; lines.sort(key=lambda l:l['source_pts_ms'])
gt=json.load(open(WORK/'gen_times.json')) if (WORK/'gen_times.json').exists() else {}
for i,l in enumerate(lines):
    out=SIGNS/f"{i:02d}.mp4"
    if out.exists() and out.stat().st_size>10000 and str(i) in gt: continue
    total=0; fs=[]
    for k,c in enumerate(chunks(l['text'])):
        f=SIGNS/f"tmp_{i:02d}_{k}.mp4"
        if not (f.exists() and f.stat().st_size>10000):
            g=signapse(c,f)
            if g is None: sys.exit(f"line {i} chunk {k} failed")
            total+=g
        fs.append(f)
    if len(fs)==1: fs[0].rename(out)
    else:
        (SIGNS/f"cat_{i}.txt").write_text("".join(f"file '{f.name}'\n" for f in fs))
        r=sh('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{i}.txt",'-c','copy',f"{i:02d}.mp4",cwd=SIGNS)
        if r.returncode: sys.exit(f"concat {i}")
    gt[str(i)]=round(total,1) if total>0 else gt.get(str(i),5.0)
    json.dump(gt,open(WORK/'gen_times.json','w'))
    print(f"sign {i} done (gen {gt[str(i)]}s)",flush=True)
print("clips complete",flush=True)
dur=vdur(BASE); s=[vdur(SIGNS/f"{i:02d}.mp4") for i in range(len(lines))]
nt=[]
for i,l in enumerate(lines):
    lag=min(max(gt.get(str(i),5.0),2.0),25.0)   # honest per-line Signapse delay, capped
    nt.append(min(l['source_pts_ms']/1000+lag, dur-2))
disp=[]
for i in range(len(lines)):
    e=nt[i]+s[i]
    if i+1<len(lines): e=min(e,nt[i+1])
    disp.append((nt[i],min(e,dur)))
W=380; XOFF=22
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
        inputs+=['-i',str(SIGNS/f"{i:02d}.mp4")]
        flt.append(f"[{nI}:v]{KEY},scale={W}:-2,setpts=PTS-STARTPTS+{nt[i]:.3f}/TB[s{i}]")
        flt.append(f"[{prev}][s{i}]overlay=x=W-w+{XOFF}:y=H-h:enable='between(t,{disp[i][0]:.2f},{disp[i][1]:.2f})'[o{i}]")
        prev=f"o{i}"
    out=str(WORK/(f"stage_{b0}.mp4" if not last else "asl_video.mp4"))
    (WORK/f"g_{b0}.txt").write_text(';'.join(flt))
    r=sh('ffmpeg','-y',*inputs,'-filter_complex_script',str(WORK/f"g_{b0}.txt"),'-map',f'[{prev}]',
        '-c:v','libx264','-preset','veryfast','-crf','20' if last else '15','-an','-t',f"{dur:.1f}",out)
    print(f"batch {b0} rc {r.returncode}",r.stderr[-150:] if r.returncode else '',flush=True)
    if r.returncode: sys.exit(1)
    cur=out
r=sh('ffmpeg','-y','-v','error','-i',str(WORK/'asl_video.mp4'),'-i',BASE,'-map','0:v','-map','1:a',
    '-c:v','copy','-c:a','copy',str(WWW/'modelE_asl.mp4'))
print("mux rc",r.returncode,"ALL DONE" if r.returncode==0 else r.stderr[-150:],flush=True)

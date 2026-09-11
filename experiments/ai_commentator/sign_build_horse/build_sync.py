#!/usr/bin/env python3
"""Synced signer version: every line's sign clip starts AT the spoken moment.
Generates any clips the live sim dropped, then composites -> horse_asl_sync.mp4"""
import json, re, subprocess, time, urllib.request, pathlib, sys
import concurrent.futures as cf
HERE=pathlib.Path(__file__).parent; SIGNS=HERE/'signs'
CLIP=str(pathlib.Path.home()/'c.mp4')
WWW=pathlib.Path('/var/www/html/experiments/ai_commentator/horse_asl')
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
IDLE='/home/ubuntu/commentary/experiments/ai_commentator/sign_build/assets/idle-jay-asl-green.mp4'
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','15','ffmpeg','-threads','2']+a[1:]
    return subprocess.run(a,capture_output=True,text=True,**k)
def vdur(f): return float(sh('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(f)).stdout.strip())
sim=json.load(open(HERE/'sim.json')); lines=sim['lines']
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
def signapse(txt,f,tries=6):
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
        except Exception: time.sleep(4)
    return False
def make_line(i):
    out=SIGNS/f"{i:02d}.mp4"
    if out.exists() and out.stat().st_size>10000: return f"{i} cached"
    cs=chunks(lines[i]['text']); fs=[]
    for k,c in enumerate(cs):
        f=SIGNS/f"tmp_{i:02d}_{k}.mp4"
        if not (f.exists() and f.stat().st_size>10000):
            if not signapse(c,f): return f"{i} FAILED"
        fs.append(f)
    if len(fs)==1: fs[0].rename(out)
    else:
        (SIGNS/f"cat_{i}.txt").write_text("".join(f"file '{x.name}'\n" for x in fs))
        sh('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{i}.txt",'-c','copy',f"{i:02d}.mp4",cwd=SIGNS)
    return f"{i} ok"
with cf.ThreadPoolExecutor(3) as ex:
    for r in ex.map(make_line, range(len(lines))): print(r,flush=True)
have=[i for i in range(len(lines)) if (SIGNS/f"{i:02d}.mp4").exists()]
print(f"clips ready: {len(have)}/{len(lines)}",flush=True)
dur=vdur(CLIP)
disp=[]
for k,i in enumerate(have):
    st=lines[i]['t']
    if st>=dur-2: break
    end=st+vdur(SIGNS/f"{i:02d}.mp4")
    nxt=[lines[j]['t'] for j in have[k+1:k+2]]
    if nxt: end=min(end,nxt[0])
    disp.append((i,st,min(end,dur)))
W=380; XOFF=22
KEY='crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green'
BATCH=10; cur=CLIP
for b0 in range(0,len(disp),BATCH):
    idx=disp[b0:b0+BATCH]; last=b0+BATCH>=len(disp)
    inputs=['-i',cur]; nin=1; flt=[]; prev='0:v'
    if b0==0:
        active='+'.join(f"between(t,{a:.2f},{b:.2f})" for _,a,b in disp)
        inputs+=['-stream_loop','-1','-t',f"{dur:.1f}",'-i',IDLE]; nin=2
        flt.append(f"[1:v]{KEY},scale={W}:-2[idle]")
        flt.append(f"[0:v][idle]overlay=x=W-w+{XOFF}:y=H-h:shortest=1:enable='if({active},0,1)'[v0]")
        prev='v0'
    for i,a,b in idx:
        nI=nin; nin+=1
        inputs+=['-i',str(SIGNS/f"{i:02d}.mp4")]
        flt.append(f"[{nI}:v]{KEY},scale={W}:-2,setpts=PTS-STARTPTS+{a:.3f}/TB[s{i}]")
        flt.append(f"[{prev}][s{i}]overlay=x=W-w+{XOFF}:y=H-h:enable='between(t,{a:.2f},{b:.2f})'[o{i}]")
        prev=f"o{i}"
    out=str(HERE/(f"sstage_{b0}.mp4" if not last else "sync_video.mp4"))
    (HERE/f"sg_{b0}.txt").write_text(';'.join(flt))
    preset='veryfast' if last else 'ultrafast'
    r=sh('ffmpeg','-y',*inputs,'-filter_complex_script',str(HERE/f"sg_{b0}.txt"),'-map',f'[{prev}]',
        '-c:v','libx264','-preset',preset,'-crf','20' if last else '15','-an','-t',f"{dur:.1f}",out)
    print(f"batch {b0} rc {r.returncode}",r.stderr[-120:] if r.returncode else '',flush=True)
    if r.returncode: sys.exit(1)
    cur=out
r=sh('ffmpeg','-y','-v','error','-i',str(HERE/'sync_video.mp4'),'-i',CLIP,'-map','0:v','-map','1:a',
    '-c:v','copy','-c:a','copy',str(WWW/'horse_asl_sync.mp4'))
print('mux rc',r.returncode,'ALL DONE' if r.returncode==0 else '',flush=True)

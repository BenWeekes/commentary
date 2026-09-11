#!/usr/bin/env python3
"""Horse-racing ASL demo: STT lines -> live-simulated Signapse signing (real measured
generation times, 3 workers, queue cap 4 drop-oldest) -> transparent overlay at TRUE
simulated ready times. Original audio kept."""
import json, re, subprocess, time, urllib.request, pathlib, sys
HERE=pathlib.Path(__file__).parent
CLIP=str(pathlib.Path.home()/'c.mp4')
WWW=pathlib.Path('/var/www/html/experiments/ai_commentator/horse_asl'); WWW.mkdir(exist_ok=True)
SIGNS=HERE/'signs'; SIGNS.mkdir(exist_ok=True)
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
IDLE='/home/ubuntu/commentary/experiments/ai_commentator/sign_build/assets/idle-jay-asl-green.mp4'
STT_LAG=1.5
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','15','ffmpeg','-threads','2']+a[1:]
    return subprocess.run(a,capture_output=True,text=True,**k)
def vdur(f): return float(sh('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(f)).stdout.strip())
# ---- lines from tokens (clean markers, sentence/gap split, merge tiny) ----
toks=[t for t in json.load(open(HERE/'tokens.json')) if t['text'] not in ('<end>','<fin>','<endpoint>')]
words=[]; buf=''; t0=None; tend=0
for t in toks:
    txt=t['text']
    if txt.startswith(' ') and buf: words.append((buf,t0,tend)); buf,t0='',None
    if buf=='': t0=t['start_ms']/1000.0
    tend=t['end_ms']/1000.0
    buf += txt.strip() if buf=='' else txt
if buf: words.append((buf,t0,tend))
lines=[]; cur=[]; start=None; last=0
for w,ts,te in words:
    if cur and ts-last>1.5: lines.append({'t':start,'end':last,'text':' '.join(cur)}); cur=[]; start=None
    if start is None: start=ts
    cur.append(w); last=te
    if re.search(r'[.!?]$',w) and len(cur)>=4:
        lines.append({'t':start,'end':last,'text':' '.join(cur)}); cur=[]; start=None
if cur: lines.append({'t':start,'end':last,'text':' '.join(cur)})
merged=[]
for l in lines:
    if merged and len(l['text'].split())<4 and l['t']-merged[-1]['end']<2:
        merged[-1]['text']+=' '+l['text']; merged[-1]['end']=l['end']
    else: merged.append(l)
lines=merged
print(len(lines),'lines',flush=True)
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
        except Exception as e: print('  retry',str(e)[:40],flush=True); time.sleep(3)
    return False
# ---- live simulation: arrivals at end+STT_LAG; 3 workers; waiting queue cap 4 ----
workers=[0.0,0.0,0.0]; waiting=[]; events=[]
def drain(now):
    while waiting:
        i=min(range(len(workers)), key=lambda k:workers[k])
        if workers[i]>now and len(waiting)<=4: break
        j=waiting.pop(0)
        f=SIGNS/f"{j:02d}.mp4"; cs=chunks(lines[j]['text']); ok=True; g0=time.time()
        for k,c in enumerate(cs):
            cf=SIGNS/f"tmp_{j:02d}_{k}.mp4"
            if not (cf.exists() and cf.stat().st_size>10000):
                if not signapse(c,cf): ok=False; break
        if ok:
            fs=[SIGNS/f"tmp_{j:02d}_{k}.mp4" for k in range(len(cs))]
            if len(fs)==1: fs[0].rename(f)
            else:
                (SIGNS/f"cat_{j}.txt").write_text("".join(f"file '{x.name}'\n" for x in fs))
                sh('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',f"cat_{j}.txt",'-c','copy',f"{j:02d}.mp4",cwd=SIGNS)
            g=time.time()-g0
            wi=min(range(len(workers)), key=lambda k:workers[k])
            start=max(workers[wi], lines[j]['end']+STT_LAG)
            workers[wi]=start+g
            events.append({'i':j,'arr':lines[j]['end']+STT_LAG,'gen':round(g,1),'ready':round(start+g,1)})
            print(f"  sign {j} gen {g:.0f}s -> ready +{start+g:.0f}s (line at {lines[j]['t']:.0f}s)",flush=True)
        else:
            events.append({'i':j,'failed':True})
for i,l in enumerate(lines):
    arr=l['end']+STT_LAG
    drain(arr)
    if len(waiting)>=4:
        d=waiting.pop(0); events.append({'i':d,'dropped':True})
        print(f"  sign {d} DROPPED (backlog)",flush=True)
    waiting.append(i)
drain(1e9)
json.dump({'lines':lines,'events':events}, open(HERE/'sim.json','w'), indent=1)
signed=[e for e in events if 'ready' in e]
print(f"signed {len(signed)}/{len(lines)} | dropped {sum(1 for e in events if e.get('dropped'))}",flush=True)
# ---- composite ----
dur=vdur(CLIP)
wins=[]
for e in sorted(signed,key=lambda x:x['ready']):
    f=SIGNS/f"{e['i']:02d}.mp4"
    if not f.exists(): continue
    wins.append([e['i'], e['ready'], vdur(f)])
disp=[]
for k,(i,st,sd) in enumerate(wins):
    if st>=dur-2: break
    end=st+sd
    if k+1<len(wins): end=min(end,wins[k+1][1])
    disp.append((i,st,min(end,dur)))
W=380; XOFF=22
KEY='crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green'
BATCH=6; cur=CLIP
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
    out=str(HERE/(f"stage_{b0}.mp4" if not last else "video_only.mp4"))
    (HERE/f"g_{b0}.txt").write_text(';'.join(flt))
    r=sh('ffmpeg','-y',*inputs,'-filter_complex_script',str(HERE/f"g_{b0}.txt"),'-map',f'[{prev}]',
        '-c:v','libx264','-preset','veryfast','-crf','20' if last else '15','-an','-t',f"{dur:.1f}",out)
    print(f"batch {b0} rc {r.returncode}",flush=True)
    if r.returncode: sys.exit(r.stderr[-200:])
    cur=out
r=sh('ffmpeg','-y','-v','error','-i',str(HERE/'video_only.mp4'),'-i',CLIP,'-map','0:v','-map','1:a',
    '-c:v','copy','-c:a','copy',str(WWW/'horse_asl.mp4'))
print('mux rc',r.returncode,'ALL DONE' if r.returncode==0 else '',flush=True)

#!/usr/bin/env python3
"""Assemble the live-run recording: EN TTS audio + transparent signer at TRUE ready times.
Output timeline = source video delayed DELAY seconds behind 'live'. Usage: <trial_id> <clip>"""
import json, subprocess, sys, pathlib
TID, CLIP = sys.argv[1:3]
AIC=pathlib.Path('/home/ubuntu/commentary/experiments/ai_commentator')
WORK=AIC/'eros_trial'/f'work_{TID}'; SIGNS=WORK/'signs'; TTS=WORK/'tts_en'
WWW=pathlib.Path(f'/var/www/html/experiments/ai_commentator/modelE_trial{TID}'); WWW.mkdir(exist_ok=True)
IDLE=str(AIC/'sign_build/assets/idle-jay-asl-green.mp4')
SR=16000; DELAY=7.0
def sh(*a,**k):
    a=list(a)
    if a[0]=='ffmpeg': a=['nice','-n','15','ffmpeg','-threads','2']+a[1:]
    return subprocess.run(a,capture_output=True,text=True,**k)
def vdur(f): return float(sh('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(f)).stdout.strip())
ev={int(k):v for k,v in json.load(open(WORK/'live_events.json')).items()}
dur=vdur(CLIP)
# audio: line audible at pts on output (arrival+tts always beat pts+DELAY; late ones shift)
track=bytearray(int(dur)*SR*2); prev=0.0; placed=[]
for i in sorted(ev):
    e=ev[i]; f=TTS/f"{i:02d}.pcm"
    if not f.exists() or 'tts_ready' not in e: continue
    d=f.stat().st_size/2/SR
    t=max(e['pts'], e['tts_ready']-DELAY, prev+0.2)
    if t-e['pts']>3.0: continue      # drop-late, don't cascade
    if t+d>dur: break
    p=int(t*SR)*2; pcm=f.read_bytes(); track[p:p+len(pcm)]=pcm
    prev=t+d; placed.append({'i':i,'t':round(t,2),'pts':e['pts'],'dur':round(d,2)})
(WORK/'track_en.pcm').write_bytes(bytes(track))
json.dump(placed,open(WORK/'placement.json','w'))
sh('ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1','-i',str(WORK/'track_en.pcm'),str(WORK/'track_en.wav'))
r=sh('python3',str(AIC/'mux_with_crowd.py'),CLIP,str(WORK/'track_en.wav'),str(WWW/'modelE_en.mp4'))
print('en mux rc',r.returncode,flush=True)
# signer: visible from (sign_ready - DELAY) on output timeline; honest live lag
wins=[]
for i in sorted(ev):
    e=ev[i]; f=SIGNS/f"{i:02d}.mp4"
    if 'sign_ready' not in e or not f.exists(): continue
    start=max(e['sign_ready']-DELAY, 0)
    wins.append([i,start,vdur(f)])
disp=[]
for k,(i,start,sd) in enumerate(wins):
    end=start+sd
    if k+1<len(wins): end=min(end,wins[k+1][1])
    disp.append((i,start,min(end,dur)))
W=380; XOFF=22
KEY='crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green'
BATCH=6; cur=str(WWW/'modelE_en.mp4')
for b0 in range(0,len(disp),BATCH):
    idx=disp[b0:b0+BATCH]; last=b0+BATCH>=len(disp)
    inputs=['-i',cur]; nin=1; flt=[]; prev2='0:v'
    if b0==0:
        active='+'.join(f"between(t,{a:.2f},{b:.2f})" for _,a,b in disp)
        inputs+=['-stream_loop','-1','-t',f"{dur:.1f}",'-i',IDLE]; nin=2
        flt.append(f"[1:v]{KEY},scale={W}:-2[idle]")
        flt.append(f"[0:v][idle]overlay=x=W-w+{XOFF}:y=H-h:shortest=1:enable='if({active},0,1)'[v0]")
        prev2='v0'
    for i,a,b in idx:
        nI=nin; nin+=1
        inputs+=['-i',str(SIGNS/f"{i:02d}.mp4")]
        flt.append(f"[{nI}:v]{KEY},scale={W}:-2,setpts=PTS-STARTPTS+{a:.3f}/TB[s{i}]")
        flt.append(f"[{prev2}][s{i}]overlay=x=W-w+{XOFF}:y=H-h:enable='between(t,{a:.2f},{b:.2f})'[o{i}]")
        prev2=f"o{i}"
    out=str(WORK/(f"stage_{b0}.mp4" if not last else "asl_video.mp4"))
    (WORK/f"g_{b0}.txt").write_text(';'.join(flt))
    r=sh('ffmpeg','-y',*inputs,'-filter_complex_script',str(WORK/f"g_{b0}.txt"),'-map',f'[{prev2}]',
        '-c:v','libx264','-preset','veryfast','-crf','20' if last else '15','-an','-t',f"{dur:.1f}",out)
    print(f"batch {b0} rc {r.returncode}",r.stderr[-120:] if r.returncode else '',flush=True)
    if r.returncode: sys.exit(1)
    cur=out
r=sh('ffmpeg','-y','-v','error','-i',str(WORK/'asl_video.mp4'),'-i',str(WWW/'modelE_en.mp4'),
    '-map','0:v','-map','1:a','-c:v','copy','-c:a','copy',str(WWW/'modelE_asl.mp4'))
print('final mux rc',r.returncode,'ALL DONE' if r.returncode==0 else '',flush=True)

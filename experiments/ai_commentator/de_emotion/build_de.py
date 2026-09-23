#!/usr/bin/env python3
"""German emotional commentary from the std clip:
Soniox EN segments (+DE draft) -> prosody features -> gpt-5.5 localizer
(ASR repair + broadcast German + guard + v3 emotion tags) -> ElevenLabs v3
(German voice, stability 0.3) -> precise assembly -> mux."""
import json, math, os, re, struct, subprocess, urllib.request, pathlib, time
import numpy as np
HERE=pathlib.Path(__file__).parent
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
SR=16000; CLIP='/home/ubuntu/commentary/clips/m05_uni_eval_25min/slice_5min.mp4'
VOICE='iQgleKwj2ZKxy6MvS4xe'
pcm=np.frombuffer(open(HERE/'audio.pcm','rb').read(),dtype=np.int16).astype(np.float32)/32768
segs=json.load(open(HERE/'segments.json'))
# merge tiny fragments into predecessor (docs: fragments trip translators)
merged=[]
for s in segs:
    if merged and len(s['en'].split())<4 and s['t']-merged[-1]['end']<1.5:
        merged[-1]['en']+=' '+s['en']; merged[-1]['de']+=' '+s['de']; merged[-1]['end']=s['end']
    else: merged.append(dict(s))
segs=merged
print(len(segs),'segments after merge',flush=True)
def prosody(a,b):
    x=pcm[int(a*SR):int(max(b,a+0.3)*SR)]
    if len(x)<400: return {}
    rms=20*math.log10(max(np.sqrt(np.mean(x**2)),1e-6))
    peak=20*math.log10(max(np.abs(x).max(),1e-6))
    # rough pitch: autocorr on 3 voiced-ish windows start/mid/end
    def f0(seg):
        if len(seg)<800: return 0
        seg=seg-seg.mean(); ac=np.correlate(seg,seg,'full')[len(seg)-1:]
        lo,hi=int(SR/350),int(SR/70)
        if hi>=len(ac): return 0
        i=lo+int(np.argmax(ac[lo:hi]))
        return SR/i if ac[i]>0.25*ac[0] else 0
    n=len(x); f=[f0(x[int(n*p):int(n*p)+1600]) for p in (0.1,0.45,0.8)]
    fv=[v for v in f if v>0]
    return {'rms_db':round(rms,1),'peak_db':round(peak,1),
            'pitch_med':round(float(np.median(fv)),0) if fv else 0,
            'pitch_rise':round((f[2]-f[0]),0) if f[0]>0 and f[2]>0 else 0}
SYSTEM="""You are localising live English football commentary (Mainz 05 vs Union Berlin, Bundesliga,
second half, 1-1) into GERMAN TV broadcast commentary, and scoring each line's emotion for
expressive TTS. You receive: the raw English STT line (may contain recognition errors), a rough
Soniox machine translation (draft only — often too literal), acoustic features of how the human
commentator SAID it (loudness dBFS, pitch, pitch rise), and your previous two German lines.

TASKS per line:
1. REPAIR obvious speech-recognition slips using football sense and the roster before translating
   (e.g. "Frenkie has been given" = "Free kick has been given"). Never invent new facts.
2. TRANSLATE into natural spoken German TV commentary — kurz, direkt, Fernsehkommentar-Register.
   GLOSSARY (binding): free kick→Freistoß · corner→Ecke · goal kick→Abstoß · throw-in→Einwurf ·
   header→Kopfball · save/catch→Parade/hält ihn fest · cross→Flanke · wide→vorbei ·
   final third→im letzten Drittel · keeps possession→lässt den Ball laufen ·
   substitution→Wechsel · booking/yellow→Gelbe Karte. Names (players, teams, referee) stay
   EXACTLY as in the source. Use "Mainz" and "Union".
3. GUARD (hard rules from our review history): NEVER invert meaning (who has the ball, on/off
   target, for/against). If the English is ambiguous or fragmentary, translate conservatively
   and literally rather than guessing. No added drama that isn't in the source.
4. EMOTION for ElevenLabs v3: pick ONE label from
   tense, anticipation, excited, urgent, big_moment, disappointment, amused, sarcastic, calm, announcing
   guided BY THE AUDIO FEATURES first (loud + high/rising pitch = excited/urgent; quiet flat =
   calm/announcing; laughter context = amused), text second.
5. TAG the German line for v3 using ONLY these audio tags (0–2 per line, placed where the
   delivery changes): [tense] [curious] [anticipation] [excited] [shouting] [urgently] [gasps]
   [sighs] [laughs] [sarcastic] [dramatically] [calm] [announcing] [applause]
   Conventions: calm analysis lines usually need NO tag or [calm]; a genuine big chance gets
   [gasps] or [shouting] and MAY use CAPS plus ! on the key word; a pause-reveal gets "..." before
   the reveal. This clip contains NO goal — do not manufacture one.
Return STRICT JSON: {"de":"<tagged German line>","emotion":"<label>","tags":["..."]}"""
def llm(payload):
    body=json.dumps({"model":"gpt-5.5",
        "response_format":{"type":"json_object"},
        "messages":[{"role":"system","content":SYSTEM},{"role":"user","content":payload}]}).encode()
    req=urllib.request.Request("https://api.openai.com/v1/chat/completions",data=body,
        headers={"Authorization":f"Bearer {ENV['OPENAI_API_KEY']}","Content-Type":"application/json"})
    for a in range(4):
        try:
            r=json.loads(urllib.request.urlopen(req,timeout=90).read())
            return json.loads(r['choices'][0]['message']['content'])
        except Exception as e:
            time.sleep(3)
    return None
out=[]; prev=[]
for i,s in enumerate(segs):
    p=prosody(s['t'],s['end'])
    payload=json.dumps({"english_stt":s['en'],"soniox_draft_de":s['de'],
        "audio_features":p,"previous_german":prev[-2:]},ensure_ascii=False)
    r=llm(payload)
    if not r: r={"de":s['de'],"emotion":"calm","tags":[]}
    out.append({'t':s['t'],'end':s['end'],'en':s['en'],'de':r['de'],
                'emotion':r.get('emotion','calm'),'tags':r.get('tags',[]),'prosody':p})
    prev.append(re.sub(r'\[[^\]]*\]','',r['de']).strip())
    print(f"[{s['t']:6.1f}] {r.get('emotion','?'):13} {r['de'][:70]}",flush=True)
json.dump(out,open(HERE/'lines_de.json','w'),ensure_ascii=False,indent=1)
# TTS v3 + precise assembly (equal-preempt, 60ms fade)
track=bytearray(300*SR*2); cur=None; report=[]
td=HERE/'tts'; td.mkdir(exist_ok=True)
for i,l in enumerate(out):
    f=td/f"{i:02d}.pcm"
    if not (f.exists() and f.stat().st_size>4000):
        body=json.dumps({"text":l['de'],"model_id":"eleven_v3","voice_settings":{"stability":0.3}}).encode()
        req=urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=pcm_16000",
            data=body,headers={"xi-api-key":ENV['ELEVENLABS_API_KEY'],"Content-Type":"application/json"})
        try: f.write_bytes(urllib.request.urlopen(req,timeout=120).read())
        except Exception as e: print(i,'tts fail',str(e)[:50],flush=True); continue
    a=f.read_bytes(); d=len(a)//2; p0=int(l['t']*SR)
    if cur and p0<cur:
        fs=max(p0-int(0.06*SR),0)
        for k in range(fs,p0):
            v=int.from_bytes(track[k*2:k*2+2],'little',signed=True)
            g=1.0-(k-fs)/max(p0-fs,1)
            track[k*2:k*2+2]=int(v*g).to_bytes(2,'little',signed=True)
        track[p0*2:cur*2]=b'\x00'*((cur-p0)*2)
        report and report[-1].update(cut=True)
    ends=min(p0+d,len(track)//2)
    track[p0*2:ends*2]=a[:(ends-p0)*2]; cur=ends
    report.append({'i':i,'played':True})
open(HERE/'track.pcm','wb').write(bytes(track))
subprocess.run(['ffmpeg','-y','-v','error','-f','s16le','-ar',str(SR),'-ac','1','-i',str(HERE/'track.pcm'),str(HERE/'track.wav')],check=True)
www=pathlib.Path('/var/www/html/experiments/ai_commentator/de_emotion'); www.mkdir(exist_ok=True)
subprocess.run(['python3','/home/ubuntu/commentary/experiments/ai_commentator/mux_with_crowd.py',CLIP,str(HERE/'track.wav'),str(www/'de_emotion.mp4')],check=True)
print('cuts:',sum(1 for r in report if r.get('cut')),'| ALL DONE',flush=True)

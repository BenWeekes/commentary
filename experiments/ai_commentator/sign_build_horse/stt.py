"""Soniox stt-rt-v5 on al.mp4 audio (4x realtime pacing), tokens -> timed sentence lines."""
import asyncio, json, re, websockets
KEY=open('/home/ubuntu/soniox').read().strip()
PCM=open('al_16k.pcm','rb').read()
CHUNK=3200*4   # 400ms of audio per send
async def run():
    toks=[]
    async with websockets.connect("wss://stt-rt.soniox.com/transcribe-websocket", max_size=None, ping_interval=20, ping_timeout=60) as ws:
        await ws.send(json.dumps({"api_key":KEY,"model":"stt-rt-v5","audio_format":"pcm_s16le",
                                  "sample_rate":16000,"num_channels":1,"enable_endpoint_detection":True}))
        async def rx():
            try:
                async for m in ws:
                    d=json.loads(m)
                    if d.get("error_code"):
                        print("SONIOX ERROR:", d["error_code"], d.get("error_message")); return
                    for t in d.get("tokens",[]):
                        if t.get("is_final"): toks.append(t)
                    if d.get("finished"):
                        print("finished marker received"); return
            except websockets.exceptions.ConnectionClosed:
                return   # server closes after the last tokens — normal end
        r=asyncio.create_task(rx())
        for i in range(0,len(PCM),CHUNK):
            await ws.send(PCM[i:i+CHUNK]); await asyncio.sleep(0.2)   # 2x realtime
        await ws.send("")   # end of audio
        try:
            await asyncio.wait_for(r, timeout=45)
        except TimeoutError:
            print(f"no finish marker; proceeding with {len(toks)} final tokens")
    json.dump(toks, open('tokens.json','w'))
    # group subword tokens -> words -> sentences
    words=[]; buf=''; t0=None
    for t in toks:
        txt=t['text']
        if txt.startswith(' ') and buf: words.append((buf,t0)); buf,t0='',None
        if buf=='' : t0=t['start_ms']/1000.0
        buf += txt.strip() if buf=='' else txt
    if buf: words.append((buf,t0))
    lines=[]; cur=[]; start=None; last_end=0
    for w,ts in words:
        if cur and (ts-last_end>1.2):
            lines.append({'t':start,'text':' '.join(cur)}); cur=[]; start=None
        if start is None: start=ts
        cur.append(w); last_end=ts
        if re.search(r'[.!?]$', w) and len(cur)>=3:
            lines.append({'t':start,'text':' '.join(cur)}); cur=[]; start=None
    if cur: lines.append({'t':start,'text':' '.join(cur)})
    json.dump(lines, open('lines.json','w'), indent=1)
    print(len(words),"words ->",len(lines),"lines")
    for l in lines[:8]: print(f"{l['t']:6.1f}  {l['text'][:80]}")
asyncio.run(run())

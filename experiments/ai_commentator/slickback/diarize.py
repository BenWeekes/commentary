import asyncio, json, websockets
KEY=open('/home/ubuntu/soniox').read().strip()
PCM=open('audio_16k.pcm','rb').read()
CHUNK=3200*2   # 200ms @2x realtime
async def run():
    toks=[]
    async with websockets.connect("wss://stt-rt.soniox.com/transcribe-websocket",
            max_size=None, ping_interval=20, ping_timeout=60) as ws:
        await ws.send(json.dumps({"api_key":KEY,"model":"stt-rt-v5","audio_format":"pcm_s16le",
            "sample_rate":16000,"num_channels":1,"enable_speaker_diarization":True,
            "enable_endpoint_detection":True}))
        async def rx():
            try:
                async for m in ws:
                    d=json.loads(m)
                    if d.get("error_code"): print("ERR",d); return
                    for t in d.get("tokens",[]):
                        if t.get("is_final"): toks.append(t)
                    if d.get("finished"): return
            except websockets.exceptions.ConnectionClosed: return
        r=asyncio.create_task(rx())
        for i in range(0,len(PCM),CHUNK):
            await ws.send(PCM[i:i+CHUNK]); await asyncio.sleep(0.1)
        await ws.send("")
        try: await asyncio.wait_for(r,timeout=45)
        except TimeoutError: print(f"timeout; {len(toks)} tokens")
    json.dump(toks, open('tokens.json','w'))
    spk=sorted({t.get('speaker') for t in toks})
    print(len(toks),"final tokens; speakers:",spk)
asyncio.run(run())

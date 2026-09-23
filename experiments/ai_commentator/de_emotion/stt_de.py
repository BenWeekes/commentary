"""Soniox stt-rt-v5 with one-way DE translation on the std clip; segments with timing."""
import asyncio, json, websockets, re
ENV=dict(l.strip().split('=',1) for l in open('/home/ubuntu/commentary/.env') if '=' in l)
PCM=open('audio.pcm','rb').read()
ROSTER=[l.strip() for l in '''Zentner,da Costa,Posch,Kohr,Caci,Sano,Mwene,Nebel,Amiri,Tietz,Becker,Weiper,Veratschnig,Kawasaki,Sieb,Maloney,Lee,Widmer,Klaus,Trimmel,Leite,Doekhi,Rothe,Khedira,Kemlein,Burke,Ansah,Burcu,Ilic,Schafer,Querfeld,Jeong,Kohn,Nsoki,Skarke,Juranovic,Kral,Mainz,Union Berlin,Mewa Arena'''.split(',')]
cfg={"api_key":ENV['SONIOX_API_KEY'],"model":"stt-rt-v5","language_hints":["en"],
 "enable_endpoint_detection":True,"audio_format":"pcm_s16le","sample_rate":16000,"num_channels":1,
 "context":{"general":[{"key":"domain","value":"Bundesliga football commentary"}],"terms":ROSTER},
 "translation":{"type":"one_way","target_language":"de"}}
toks=[]
async def run():
    async with websockets.connect("wss://stt-rt.soniox.com/transcribe-websocket",max_size=None,ping_interval=20,ping_timeout=60) as ws:
        await ws.send(json.dumps(cfg))
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
        for i in range(0,len(PCM),3200*2):
            await ws.send(PCM[i:i+3200*2]); await asyncio.sleep(0.2)
        await ws.send("")
        try: await asyncio.wait_for(r,timeout=60)
        except TimeoutError: print("no finish marker; using",len(toks),"tokens")
asyncio.run(run())
json.dump(toks,open('tokens_de.json','w'))
# pair EN segments with their DE translations, keep source timing
segs=[]; cur_en=[]; cur_de=[]; t0=None; t1=0
def flush():
    global cur_en,cur_de,t0,t1
    en=''.join(x['text'] for x in cur_en).strip(); de=''.join(x['text'] for x in cur_de).strip()
    if en and t0 is not None: segs.append({'t':round(t0/1000,2),'end':round(t1/1000,2),'en':en,'de':de})
    cur_en=[];cur_de=[];t0=None
for t in toks:
    txt=t.get('text','')
    if txt in ('<end>','<fin>','<endpoint>'):
        flush(); continue
    st=t.get('translation_status')
    if st in (None,'original','none'):
        if t0 is None and t.get('start_ms') is not None: t0=t['start_ms']
        if t.get('end_ms'): t1=t['end_ms']
        cur_en.append(t)
    elif st=='translation': cur_de.append(t)
flush()
json.dump(segs,open('segments.json','w'),ensure_ascii=False,indent=1)
print(len(segs),"segments")
for s in segs[:5]: print(f"[{s['t']:5.1f}] EN: {s['en'][:50]} | DE: {s['de'][:50]}")

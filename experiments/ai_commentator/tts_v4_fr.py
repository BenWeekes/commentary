#!/usr/bin/env python3
"""Translate v4 EN tagged lines to FR, TTS via eleven_v3 with FR voice."""
import os, json, wave, time, re, urllib.request
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

EL_KEY = os.environ['ELEVENLABS_API_KEY']
VOICE = 'LcKoSBj8CeBInl4bQHtq'  # Keith - FR
MODEL = 'eleven_v3'
SR = 16000
DURATION_S = 300.0
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
NATURAL_LAG_S = 0.3

TRANSLATE_SYSTEM = """You translate English live football commentary into
natural French. Keep it concise — same length or shorter. Use idiomatic
sports-commentary French. Preserve names exactly. Do NOT translate proper
nouns (player names, place names like Köpenick, manager names).

Output ONLY the French translation, nothing else."""

def translate(en):
    resp = client.chat.completions.create(
        model='gpt-5.4', reasoning_effort='low', max_completion_tokens=400,
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": en},
        ],
    )
    return (resp.choices[0].message.content or '').strip().strip('"')

def tts(text, model_id):
    body = json.dumps({"text": text, "model_id": model_id,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=pcm_16000",
        data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

rows = [json.loads(l) for l in open(BASE / 'commentary_v4_en_tagged.jsonl')]
print(f"Translating + TTS-ing {len(rows)} lines to French")

silence = bytearray(int(DURATION_S * SR * 2))
out_log = []
t0 = time.time()
fallbacks = 0
for i, r in enumerate(rows):
    try:
        fr = translate(r['text'])
    except Exception as e:
        print(f"  [{i}] translate failed: {e}")
        fr = r['text']  # fall through with EN
    tagged_fr = f"{r['tag']} {fr}"
    try:
        pcm = tts(tagged_fr, MODEL)
        model_used = MODEL
    except Exception as e:
        print(f"  [{i}] {MODEL} failed: {e} — fall back")
        pcm = tts(tagged_fr, 'eleven_flash_v2_5')
        model_used = 'eleven_flash_v2_5'
        fallbacks += 1
    start_s = r['video_time_s'] + NATURAL_LAG_S
    start_byte = int(start_s * SR) * 2
    if start_byte < len(silence):
        usable = min(len(pcm), len(silence) - start_byte)
        if usable > 0:
            silence[start_byte:start_byte+usable] = pcm[:usable]
    out_log.append({**r, 'fr': fr, 'tagged_fr': tagged_fr, 'fr_start_s': start_s, 'model_used': model_used})
    if i % 10 == 0 or i == len(rows)-1:
        print(f"  [{i:2d}/{len(rows)}] {time.time()-t0:>4.0f}s {r['tag']:<18} {fr[:65]!r}")

with wave.open(str(BASE / 'ai_commentary_v4_fr_track.wav'), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(bytes(silence))
with open(BASE / 'commentary_v4_fr_tagged.jsonl', 'w') as f:
    for r in out_log:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print(f"\nFallbacks: {fallbacks}, total {time.time()-t0:.0f}s")

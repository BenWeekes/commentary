#!/usr/bin/env python3
"""TTS EN + FR from v8 output. FR is already generated inline by the arbiter,
so we only pick tags + TTS (no re-translate call)."""
import os, sys, json, wave, time, re, urllib.request
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from openai import OpenAI
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

EL_KEY = os.environ['ELEVENLABS_API_KEY']
EN_VOICE = 'gU0LNdkMOQCOrPrwtbee'
FR_VOICE = 'LcKoSBj8CeBInl4bQHtq'
MODEL = 'eleven_v3'
SR = 16000
DURATION_S = 300.0
NATURAL_LAG_S = 0.3

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')

TAG_SYSTEM = """Tag a football commentary line for expressive TTS.
Pick ONE tag from: [calm] [flatly] [excited] [nervous] [frustrated] [sorrowful]
[resigned tone] [whispers] [deadpan] [cheerfully]. Default to [calm]/[flatly].
Output ONLY the tag in brackets."""

def pick_tag(text):
    resp = client.chat.completions.create(
        model='gpt-5.4', reasoning_effort='low', max_completion_tokens=200,
        messages=[{"role": "system", "content": TAG_SYSTEM},
                  {"role": "user", "content": text}])
    raw = (resp.choices[0].message.content or '').strip()
    m = re.search(r'\[[a-z ]+\]', raw)
    return m.group(0) if m else '[calm]'

def tts(text, voice, model_id=MODEL):
    body = json.dumps({"text": text, "model_id": model_id,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

rows = [json.loads(l) for l in open(BASE / 'commentary_v8_scheduled.jsonl')]
print(f"Tagging + TTS {len(rows)} rows")

# Pass 1: tag
for i, r in enumerate(rows):
    try:
        r['tag'] = pick_tag(r['text'])
    except Exception as e:
        print(f"  tag [{i}] fail: {e}")
        r['tag'] = '[calm]'
    if i % 10 == 0: print(f"  tagged {i}/{len(rows)}: {r['tag']}")

from collections import Counter
print(f"Tag distribution: {dict(Counter(r['tag'] for r in rows))}")

# Pass 2: EN TTS
en_audio = bytearray(int(DURATION_S * SR * 2))
t0 = time.time()
for i, r in enumerate(rows):
    text = f"{r['tag']} {r['text']}"
    try:
        pcm = tts(text, EN_VOICE)
    except Exception as e:
        print(f"  EN [{i}] eleven_v3 fail: {e}; fallback")
        pcm = tts(text, EN_VOICE, 'eleven_flash_v2_5')
    start_s = r['video_time_s'] + NATURAL_LAG_S
    b = int(start_s * SR) * 2
    if b < len(en_audio):
        u = min(len(pcm), len(en_audio) - b)
        if u > 0: en_audio[b:b+u] = pcm[:u]
    if i % 10 == 0: print(f"  EN [{i}/{len(rows)}] {time.time()-t0:.0f}s {text[:60]!r}")

with wave.open(str(BASE / 'ai_commentary_v8_en_track.wav'), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(bytes(en_audio))

# Pass 3: FR TTS (use pre-generated FR from arbiter)
fr_audio = bytearray(int(DURATION_S * SR * 2))
t0 = time.time()
for i, r in enumerate(rows):
    fr = r.get('fr') or r['text']
    text = f"{r['tag']} {fr}"
    try:
        pcm = tts(text, FR_VOICE)
    except Exception as e:
        print(f"  FR [{i}] eleven_v3 fail: {e}; fallback")
        pcm = tts(text, FR_VOICE, 'eleven_flash_v2_5')
    start_s = r['video_time_s'] + NATURAL_LAG_S
    b = int(start_s * SR) * 2
    if b < len(fr_audio):
        u = min(len(pcm), len(fr_audio) - b)
        if u > 0: fr_audio[b:b+u] = pcm[:u]
    if i % 10 == 0: print(f"  FR [{i}/{len(rows)}] {time.time()-t0:.0f}s {fr[:60]!r}")

with wave.open(str(BASE / 'ai_commentary_v8_fr_track.wav'), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(bytes(fr_audio))

# Save tagged JSONL for the results page
with open(BASE / 'commentary_v8_en_tagged.jsonl', 'w') as f:
    for r in rows: f.write(json.dumps({**r, 'natural_start_s': r['video_time_s'] + NATURAL_LAG_S}, ensure_ascii=False) + '\n')
with open(BASE / 'commentary_v8_fr_tagged.jsonl', 'w') as f:
    for r in rows: f.write(json.dumps({**r, 'natural_start_s': r['video_time_s'] + NATURAL_LAG_S, 'text': r.get('fr','')}, ensure_ascii=False) + '\n')
print("Done.")

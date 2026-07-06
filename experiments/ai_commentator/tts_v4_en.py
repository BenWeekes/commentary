#!/usr/bin/env python3
"""Tag each v4 EN line + TTS via eleven_v3, output WAV track + tagged JSONL."""
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
VOICE = 'gU0LNdkMOQCOrPrwtbee'      # British Football Announcer
MODEL = 'eleven_v3'
SR = 16000
DURATION_S = 300.0
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
NATURAL_LAG_S = 0.3

TAG_SYSTEM = """Tag a single live football commentary line for expressive TTS.

Choose ONE tag from this list to prepend to the line:
  [calm]          default — routine description, build-up, set pieces
  [flatly]        neutral factual outcome / short statement
  [excited]       fast attack, shot, breakaway, dramatic build
  [nervous]       suspense, keeper under pressure, near miss
  [frustrated]    botched chance, defensive error, blocked play
  [sorrowful]     injury stoppage, player down, dejection
  [resigned tone] settled-down stoppage, substitution off, late accepting
  [whispers]      rare — only for tense quiet pauses
  [deadpan]       dry score/time reference
  [cheerfully]    light colour, positive anticipation

Football commentary is mostly RESTRAINED. Default toward [calm] or [flatly].
Output ONLY the tag in brackets, nothing else."""

def pick_tag(text):
    resp = client.chat.completions.create(
        model='gpt-5.4', reasoning_effort='low', max_completion_tokens=200,
        messages=[
            {"role": "system", "content": TAG_SYSTEM},
            {"role": "user", "content": text},
        ],
    )
    raw = (resp.choices[0].message.content or '').strip()
    m = re.search(r'\[[a-z ]+\]', raw)
    return m.group(0) if m else '[calm]'

def tts(text, model_id):
    body = json.dumps({"text": text, "model_id": model_id,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=pcm_16000",
        data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

rows = [json.loads(l) for l in open(BASE / 'commentary_v4_scheduled.jsonl')]
print(f"Re-rendering {len(rows)} lines with model={MODEL}, voice={VOICE}")

silence = bytearray(int(DURATION_S * SR * 2))
tagged_log = []
t0 = time.time()
for i, r in enumerate(rows):
    en = r['text']
    try:
        tag = pick_tag(en)
    except Exception as e:
        print(f"  [{i}] tag pick failed: {e} — default [calm]")
        tag = '[calm]'
    tagged_text = f"{tag} {en}"
    try:
        pcm = tts(tagged_text, MODEL)
        model_used = MODEL
    except Exception as e:
        print(f"  [{i}] TTS error: {e} — fall back")
        pcm = tts(tagged_text, 'eleven_flash_v2_5')
        model_used = 'eleven_flash_v2_5'
    natural_start_s = r['video_time_s'] + NATURAL_LAG_S
    start_byte = int(natural_start_s * SR) * 2
    if start_byte < len(silence):
        usable = min(len(pcm), len(silence) - start_byte)
        if usable > 0:
            silence[start_byte:start_byte+usable] = pcm[:usable]
    tagged_log.append({**r, 'tag': tag, 'tagged_text': tagged_text,
                       'natural_start_s': natural_start_s, 'model_used': model_used})
    if i % 10 == 0 or i == len(rows)-1:
        print(f"  [{i:2d}/{len(rows)}] {time.time()-t0:>4.0f}s {tag:<18} {en[:65]!r}")

with wave.open(str(BASE / 'ai_commentary_v4_en_track.wav'), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(bytes(silence))
with open(BASE / 'commentary_v4_en_tagged.jsonl', 'w') as f:
    for r in tagged_log:
        f.write(json.dumps(r) + '\n')

from collections import Counter
print(f"\nTag distribution:")
for tag, c in Counter(r['tag'] for r in tagged_log).most_common():
    print(f"  {tag:<22} {c}")
print(f"Done in {time.time()-t0:.0f}s")

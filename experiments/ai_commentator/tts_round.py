#!/usr/bin/env python3
"""Tag + TTS one EN run (v5 or gemini) and optionally produce FR.

Usage:
  python tts_round.py <input_jsonl> <out_en_track.wav> <out_en_tagged.jsonl> [<out_fr_track.wav> <out_fr_tagged.jsonl>]
"""
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

TAG_SYSTEM = """Tag a single live football commentary line for expressive TTS.

Choose ONE tag from this list to prepend:
  [calm] [flatly] [excited] [nervous] [frustrated] [sorrowful]
  [resigned tone] [whispers] [deadpan] [cheerfully]

Football commentary is mostly RESTRAINED. Default toward [calm] or [flatly].
Output ONLY the tag in brackets, nothing else."""

TRANSLATE_SYSTEM = """Translate English live football commentary into natural
French. Concise, idiomatic sports French. Preserve player names, place names,
manager names exactly. Output ONLY the French translation."""


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


def translate(en):
    resp = client.chat.completions.create(
        model='gpt-5.4', reasoning_effort='low', max_completion_tokens=400,
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": en},
        ],
    )
    return (resp.choices[0].message.content or '').strip().strip('"')


def tts(text, voice, model_id):
    body = json.dumps({"text": text, "model_id": model_id,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def render_track(rows, voice, lang_key, fallback_text_key):
    silence = bytearray(int(DURATION_S * SR * 2))
    out_log = []
    fallbacks = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        text = r.get(lang_key) or r.get(fallback_text_key) or ''
        tag = r.get('tag', '[calm]')
        full = f"{tag} {text}"
        try:
            pcm = tts(full, voice, MODEL)
            model_used = MODEL
        except Exception as e:
            print(f"  [{i}] {MODEL} fail: {e}; fallback")
            pcm = tts(full, voice, 'eleven_flash_v2_5')
            model_used = 'eleven_flash_v2_5'
            fallbacks += 1
        start_s = r['video_time_s'] + NATURAL_LAG_S
        start_byte = int(start_s * SR) * 2
        if start_byte < len(silence):
            usable = min(len(pcm), len(silence) - start_byte)
            if usable > 0:
                silence[start_byte:start_byte+usable] = pcm[:usable]
        out_log.append({**r, 'tagged_text': full, 'natural_start_s': start_s, 'model_used': model_used})
        if i % 10 == 0:
            print(f"  [{i:2d}/{len(rows)}] {time.time()-t0:>4.0f}s {tag:<18} {text[:60]!r}")
    return bytes(silence), out_log, fallbacks


def main(argv):
    inp = Path(argv[1])
    out_en_wav = Path(argv[2])
    out_en_jsonl = Path(argv[3])
    out_fr_wav = Path(argv[4]) if len(argv) > 5 else None
    out_fr_jsonl = Path(argv[5]) if len(argv) > 5 else None

    rows = [json.loads(l) for l in open(inp)]
    print(f"Loading {len(rows)} rows from {inp.name}")

    # Tag each line first
    print("=== picking tags ===")
    for i, r in enumerate(rows):
        try:
            r['tag'] = pick_tag(r['text'])
        except Exception as e:
            print(f"  tag pick fail [{i}]: {e}")
            r['tag'] = '[calm]'
    from collections import Counter
    print("Tag distribution:", dict(Counter(r['tag'] for r in rows).most_common()))

    print("=== EN TTS ===")
    en_audio, en_log, en_fb = render_track(rows, EN_VOICE, 'text', 'text')
    with wave.open(str(out_en_wav), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(en_audio)
    with open(out_en_jsonl, 'w') as f:
        for r in en_log: f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f"EN done, fallbacks={en_fb}")

    if out_fr_wav:
        print("=== translating + FR TTS ===")
        # add fr translation
        for i, r in enumerate(rows):
            try:
                r['fr'] = translate(r['text'])
            except Exception as e:
                print(f"  translate fail [{i}]: {e}")
                r['fr'] = r['text']
        fr_audio, fr_log, fr_fb = render_track(rows, FR_VOICE, 'fr', 'text')
        with wave.open(str(out_fr_wav), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(fr_audio)
        with open(out_fr_jsonl, 'w') as f:
            for r in fr_log: f.write(json.dumps(r, ensure_ascii=False) + '\n')
        print(f"FR done, fallbacks={fr_fb}")


if __name__ == '__main__':
    main(sys.argv)

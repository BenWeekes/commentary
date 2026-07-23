#!/usr/bin/env python3
"""Standalone: Gemini 3.5 speech models vs Soniox on the 5-min football clip.

NOTE: the allowlisted preview models `models/gemini-3.5-transcribe-preview` and
`models/gemini-3.5-transcribe-live-preview` return 404 on our API key (this key's
Cloud project is not the allowlisted one). This script therefore uses the reachable
3.5 models as the closest stand-ins:
  - UNARY transcription : models/gemini-3.5-flash        (generateContent + audio)
  - LIVE  translate/asr : models/gemini-3.5-live-translate-preview (bidiGenerateContent)
Swap MODEL_UNARY / MODEL_LIVE to the transcribe-preview strings once a key from the
allowlisted project is available — the rest of the harness is unchanged.

Usage:
  .venv/bin/python test_gemini_transcribe.py unary   # full-clip transcription + accuracy
  .venv/bin/python test_gemini_transcribe.py live     # streaming translate + latency
"""
import os, sys, time, json, re, wave
from pathlib import Path

for _l in open('/home/ubuntu/commentary/.env'):
    if _l.startswith('GEMINI_API_KEY'):
        os.environ['GEMINI_API_KEY'] = _l.split('=', 1)[1].strip().strip('"')

from google import genai
from google.genai import types

CLIP = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/clip_16k.wav')
GOLD = Path('/home/ubuntu/commentary/experiments/ai_commentator/gold_soniox_5min.jsonl')
OUT = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad')
MODEL_UNARY = 'models/gemini-3.5-flash'                    # -> gemini-3.5-transcribe-preview when allowlisted
MODEL_LIVE = 'models/gemini-3.5-live-translate-preview'    # -> gemini-3.5-transcribe-live-preview when allowlisted
client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])

_WS = re.compile(r'[^a-z0-9 ]')
def norm(s):
    return _WS.sub('', s.lower()).split()

def wer(ref_words, hyp_words):
    # Levenshtein on word lists
    n, m = len(ref_words), len(hyp_words)
    if n == 0:
        return 1.0
    d = list(range(m + 1))
    for i in range(1, n + 1):
        prev, d[0] = d[0], i
        for j in range(1, m + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j-1] + 1, prev + (ref_words[i-1] != hyp_words[j-1]))
            prev = cur
    return d[m] / n

def gold_text():
    return ' '.join(json.loads(l)['text'] for l in open(GOLD) if l.strip())

def unary():
    dur = wave.open(str(CLIP)).getnframes() / 16000
    print(f"UNARY transcription — {MODEL_UNARY} on {dur:.0f}s clip")
    up = client.files.upload(file=str(CLIP))
    while up.state.name == 'PROCESSING':
        time.sleep(1); up = client.files.get(name=up.name)
    t0 = time.time()
    r = client.models.generate_content(
        model=MODEL_UNARY,
        contents=['Transcribe this football commentary verbatim, exactly as spoken. '
                  'Output only the transcript text, no timestamps, no speaker labels.', up])
    dt = time.time() - t0
    hyp = re.sub(r'\s+', ' ', (r.text or '').strip())
    (OUT / 'gemini_unary.txt').write_text(hyp)
    ref = gold_text()
    w = wer(norm(ref), norm(hyp))
    print(f"  latency (batch, full clip): {dt:.2f}s  ({dt/dur*100:.0f}% of realtime)")
    print(f"  words: gemini={len(norm(hyp))}  gold/soniox={len(norm(ref))}")
    print(f"  WER(gemini vs gold_soniox): {w:.3f}  (lower = closer to the reference)")
    print(f"  --- first 400 chars ---\n  {hyp[:400]}")
    print(f"\n  --- gold_soniox first 400 ---\n  {ref[:400]}")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'unary'
    if mode == 'unary':
        unary()
    else:
        print("live mode is in the next step")

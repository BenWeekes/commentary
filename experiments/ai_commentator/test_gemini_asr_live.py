#!/usr/bin/env python3
"""Gemini 3.5 Transcribe Live (EAP) vs Soniox gold — speed + accuracy on the 5-min clip.

Two real-time-paced streaming runs over the same audio the Soniox gold covers:
  A) baseline (language_auto)
  B) adaptation_phrases = roster surnames + team words  (custom-vocab hit rate)

Latency: transcripts arrive on a live socket while audio streams at 1x; the elapsed
stream clock maps 1:1 to clip time, so per-segment lag = arrival_elapsed - gold_end_s
of the best-matching gold segment (EAP target: <1s P90 from utterance end).
Accuracy: WER vs gold (gold itself is unverified — disagreements are listed with
timestamps for human adjudication, not auto-declared wrong).

Usage: EAP_KEY=... .venv/bin/python test_gemini_asr_live.py [--adapt] [--tag NAME]
"""
import asyncio, difflib, json, os, re, sys, time, wave
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
WAV = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/clip_16k.wav')
GOLD = BASE / 'gold_soniox_5min.jsonl'
OUT = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/gemini_asr')
OUT.mkdir(parents=True, exist_ok=True)
MODEL = 'gemini-3.5-transcribe-live-preview'

from google import genai
from google.genai import types

sys.path.insert(0, str(BASE))


def roster_phrases():
    import run_blend_live as B
    names = sorted({p['name'] for p in B.ALL_PLAYERS})
    extra = ['Mainz', 'Union Berlin', 'Mewa Arena', 'Bundesliga', 'Zentner', 'Rønnow']
    return names + [e for e in extra if e not in names]


async def stream_run(adapt, tag):
    client = genai.Client(api_key=os.environ['EAP_KEY'])
    cfg_kwargs = dict(language_auto={})
    if adapt:
        cfg_kwargs['adaptation_phrases'] = roster_phrases()
    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(**cfg_kwargs),
    )
    w = wave.open(str(WAV)); rate = w.getframerate()
    data = w.readframes(w.getnframes())
    events = []                      # (elapsed_s_at_arrival, text)
    t0 = None

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        async def send():
            step = 1024 * 2          # 1024 frames of 16-bit mono
            for i in range(0, len(data), step):
                await session.send_realtime_input(
                    audio=types.Blob(data=data[i:i + step], mime_type=f"audio/pcm;rate={rate}"))
                await asyncio.sleep(1024 / rate)
            await session.send_realtime_input(audio_stream_end=True)

        async def recv():
            async for m in session.receive():
                sc = m.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    events.append((time.monotonic() - t0, sc.input_transcription.text))

        t0 = time.monotonic()
        rtask = asyncio.create_task(recv())
        await send()
        try:
            await asyncio.wait_for(rtask, timeout=10)
        except asyncio.TimeoutError:
            rtask.cancel()
    (OUT / f'{tag}_events.json').write_text(json.dumps(events, indent=1))
    return events


_WSRX = re.compile(r"[^a-z0-9' ]")
def norm(s):
    return _WSRX.sub(' ', s.lower()).split()


def wer(ref, hyp):
    n, m = len(ref), len(hyp)
    if n == 0:
        return 1.0
    d = list(range(m + 1))
    for i in range(1, n + 1):
        prev, d[0] = d[0], i
        for j in range(1, m + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return d[m] / n


def analyse(events, tag):
    gold = [json.loads(l) for l in open(GOLD) if l.strip()]
    full = ' '.join(t for _, t in events)
    gold_full = ' '.join(g['text'] for g in gold)
    w = wer(norm(gold_full), norm(full))
    # latency: match each event's text against gold segments; lag vs gold end_s
    lags = []
    for arr, txt in events:
        toks = norm(txt)
        if len(toks) < 3:
            continue
        best, best_r = None, 0.0
        for g in gold:
            r = difflib.SequenceMatcher(None, toks, norm(g['text'])).ratio()
            if r > best_r:
                best_r, best = r, g
        if best and best_r > 0.5 and abs(arr - best['end_s']) < 15:
            lags.append(arr - best['end_s'])
    lags.sort()
    lat = {'n_matched': len(lags),
           'p50': round(lags[len(lags)//2], 2) if lags else None,
           'p90': round(lags[int(len(lags)*0.9)], 2) if lags else None,
           'max': round(lags[-1], 2) if lags else None}
    res = {'tag': tag, 'segments': len(events), 'words': len(norm(full)),
           'gold_words': len(norm(gold_full)), 'wer_vs_gold': round(w, 3), 'latency_s': lat}
    print(json.dumps(res, indent=1))
    (OUT / f'{tag}_summary.json').write_text(json.dumps(res, indent=1))
    return res


if __name__ == '__main__':
    adapt = '--adapt' in sys.argv
    tag = sys.argv[sys.argv.index('--tag') + 1] if '--tag' in sys.argv else ('adapt' if adapt else 'base')
    ev = asyncio.run(stream_run(adapt, tag))
    analyse(ev, tag)

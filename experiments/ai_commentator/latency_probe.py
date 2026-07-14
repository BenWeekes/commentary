#!/usr/bin/env python3
"""Standalone gpt-5.5 vision latency probe — isolates the live pipeline's
"slow-call tail" from all the SRT / TTS / loop machinery.

It fires N sequential vision calls that are byte-for-byte the same shape as
live_srt_run.py's vision_call(): MODEL, reasoning={"effort":"low"},
max_output_tokens=200, one text prompt + 5 real JPEG frames. Each call's wall
latency is logged, then a distribution (min/median/p90/max, count>15s) is printed.

Purpose: run it a few times HERE and on the OTHER server. If the ~33 s tail
reproduces here but not there, the difference is environmental (region / key
tier / routing), not our code.

Env overrides (so both servers run an identical probe):
  PROBE_MODEL        default gpt-5.5
  PROBE_N            default 20      (calls to make)
  PROBE_FRAMES       default 5       (images per call)
  PROBE_EFFORT       default low     (reasoning effort for gpt-5.5)
  PROBE_MAX_TOKENS   default 200
  PROBE_CONCURRENCY  default 1       (1 = sequential; >1 = fire in parallel)

Usage:
  /home/ubuntu/commentary/.venv/bin/python latency_probe.py
"""
import os, sys, time, base64, json, statistics, concurrent.futures
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'

# --- load .env exactly like the other scripts ---
for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

MODEL       = os.environ.get('PROBE_MODEL', 'gpt-5.5')
N           = int(os.environ.get('PROBE_N', '20'))
NFRAMES     = int(os.environ.get('PROBE_FRAMES', '5'))
EFFORT      = os.environ.get('PROBE_EFFORT', 'low')
MAX_TOKENS  = int(os.environ.get('PROBE_MAX_TOKENS', '200'))
CONCURRENCY = int(os.environ.get('PROBE_CONCURRENCY', '1'))

PROMPT = (
    "You are a live English football play-by-play commentator. You see a short "
    "burst of frames, oldest first, newest last. Comment on the newest frame in "
    "3-12 words, or reply NO_CALL if nothing of consequence is happening."
)


def encode_jpeg(p):
    return base64.b64encode(Path(p).read_bytes()).decode()


def one_call(frame_paths):
    content = [{"type": "input_text", "text": PROMPT}]
    for p in frame_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    kwargs = dict(model=MODEL,
                  input=[{"role": "user", "content": content}],
                  max_output_tokens=MAX_TOKENS)
    if MODEL.startswith('gpt-5.5'):
        kwargs['reasoning'] = {"effort": EFFORT}
    else:
        kwargs['temperature'] = 0.55
    t0 = time.monotonic()
    err = None
    try:
        resp = client.responses.create(**kwargs)
        txt = (resp.output_text or '').strip()
    except Exception as e:
        txt = ''
        err = f"{type(e).__name__}: {e}"
    ms = int((time.monotonic() - t0) * 1000)
    return ms, txt, err


def main():
    all_frames = sorted(FRAMES_DIR.glob('f_*.jpg'))
    if len(all_frames) < NFRAMES:
        print(f"need >= {NFRAMES} frames in {FRAMES_DIR}"); sys.exit(1)
    # Spread the N bursts across the clip so we sample varied content.
    step = max(1, (len(all_frames) - NFRAMES) // max(1, N))
    bursts = [all_frames[i*step : i*step + NFRAMES] for i in range(N)]

    print(f"probe: model={MODEL} effort={EFFORT} max_tokens={MAX_TOKENS} "
          f"frames/call={NFRAMES} calls={N} concurrency={CONCURRENCY}")
    lats = []
    errs = 0

    if CONCURRENCY <= 1:
        for i, burst in enumerate(bursts):
            ms, txt, err = one_call(burst)
            lats.append(ms)
            if err:
                errs += 1
                print(f"  [{i:2d}] {ms:6d}ms  ERR {err[:120]}")
            else:
                print(f"  [{i:2d}] {ms:6d}ms  {txt[:60]!r}")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(one_call, b): i for i, b in enumerate(bursts)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                ms, txt, err = fut.result()
                lats.append(ms)
                if err:
                    errs += 1
                    print(f"  [{i:2d}] {ms:6d}ms  ERR {err[:120]}")
                else:
                    print(f"  [{i:2d}] {ms:6d}ms  {txt[:60]!r}")

    lats.sort()
    def pct(p):
        return lats[min(len(lats)-1, int(len(lats)*p))]
    print("\n=== distribution (ms) ===")
    print(f"  n={len(lats)}  errors={errs}")
    print(f"  min={lats[0]}  median={statistics.median(lats):.0f}  "
          f"p90={pct(0.9)}  max={lats[-1]}")
    print(f"  calls > 15s: {sum(1 for x in lats if x > 15000)}   "
          f"> 25s: {sum(1 for x in lats if x > 25000)}")


if __name__ == '__main__':
    main()

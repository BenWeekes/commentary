#!/usr/bin/env python3
"""Unified vision-eval runner — same events_detector prompt to OpenAI and Gemini.

Runs prompts/events_detector_v1.txt against burst-windows of frames/*.jpg on the
chosen provider/model, concurrently, and writes strict-JSON detections to
events_<tag>.jsonl (same record shape as run_events_detector.py) so the
comparison page can line them up on the video clock.

Usage:
  # OpenAI gpt-5.5, stride 2 (~270 bursts):
  ./run_vision_eval.py --provider openai --model gpt-5.5 --stride 2 --tag gpt55
  # Gemini flash:
  ./run_vision_eval.py --provider gemini --model gemini-3-flash-preview --stride 2 --tag gemini_flash
  # smoke test:
  ./run_vision_eval.py --provider gemini --model gemini-3-flash-preview --limit 3
"""
from __future__ import annotations
import argparse, base64, json, os, re, sys, time, urllib.request, concurrent.futures
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
PROMPT_PATH = BASE / 'prompts' / 'events_detector_v1.txt'
SAMPLE_INTERVAL_S = 0.55
BURST_SIZE = 4
MAX_OUTPUT_TOKENS = 800
JSON_RE = re.compile(r'\{.*\}', re.DOTALL)
REQUIRED_TOP = {'phase', 'possession', 'ball_state', 'events'}


def encode_jpeg(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode('ascii')


# ---- providers ----------------------------------------------------------

def call_openai(model, prompt, burst_paths):
    from openai import OpenAI
    client = call_openai._client or OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    call_openai._client = client
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    resp = client.responses.create(model=model,
        input=[{"role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS, reasoning={"effort": "low"})
    return (resp.output_text or '').strip()
call_openai._client = None


def call_gemini(model, prompt, burst_paths):
    key = os.environ['GEMINI_API_KEY']
    parts = [{"text": prompt}]
    for p in burst_paths:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": encode_jpeg(p)}})
    # gemini-3-flash is a thinking model — thinking consumes the token budget,
    # so give generous headroom and force clean JSON output.
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096,
                             "responseMimeType": "application/json"},
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    cands = d.get('candidates', [])
    if not cands:
        return ''
    return ''.join(pt.get('text', '') for pt in cands[0].get('content', {}).get('parts', [])).strip()


def extract_json(raw):
    if not raw:
        return None, 'empty'
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?', '', raw).rstrip('`').strip()
    m = JSON_RE.search(raw)
    if not m:
        return None, 'no_json'
    try:
        return json.loads(m.group(0)), None
    except json.JSONDecodeError as e:
        return None, f'json_decode: {e}'


def validate_shape(obj):
    if not isinstance(obj, dict):
        return 'not_dict'
    if REQUIRED_TOP - obj.keys():
        return f'missing: {sorted(REQUIRED_TOP - obj.keys())}'
    if not isinstance(obj.get('events'), list):
        return 'events_not_list'
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--provider', required=True, choices=['openai', 'gemini'])
    ap.add_argument('--model', required=True)
    ap.add_argument('--tag', default=None, help='output tag (events_<tag>.jsonl)')
    ap.add_argument('--stride', type=int, default=2)
    ap.add_argument('--frames', type=int, default=BURST_SIZE,
                    help='burst size = frames of history per call (temporal window = frames*0.55s)')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--concurrency', type=int, default=6)
    args = ap.parse_args()
    burst_size = args.frames
    tag = args.tag or args.model.replace('.', '').replace('-', '_')
    out_path = BASE / f'events_{tag}.jsonl'
    caller = call_openai if args.provider == 'openai' else call_gemini

    prompt = PROMPT_PATH.read_text()
    frames = sorted(FRAMES_DIR.glob('f_*.jpg'))
    bursts = []
    for i in range(burst_size - 1, len(frames), args.stride):
        bursts.append((i, round((i + 1) * SAMPLE_INTERVAL_S, 2), frames[i - burst_size + 1: i + 1]))
    if args.limit:
        bursts = bursts[:args.limit]
    print(f"provider={args.provider} model={args.model} bursts={len(bursts)} "
          f"stride={args.stride} concurrency={args.concurrency} -> {out_path.name}")

    def do(item):
        frame_i, vts, burst = item
        rec = {'burst_i': frame_i, 'video_time_s': vts,
               'frames': [p.name for p in burst]}
        t0 = time.monotonic()
        try:
            raw = caller(args.model, prompt, burst)
        except Exception as e:
            rec['error'] = f'{type(e).__name__}: {str(e)[:200]}'
            rec['latency_ms'] = int((time.monotonic() - t0) * 1000)
            return rec
        rec['latency_ms'] = int((time.monotonic() - t0) * 1000)
        obj, perr = extract_json(raw)
        if perr:
            rec['error'] = f'parse: {perr}'; rec['raw'] = raw[:300]; return rec
        serr = validate_shape(obj)
        if serr:
            rec['error'] = f'shape: {serr}'; rec['detection'] = obj; return rec
        rec['detection'] = obj
        return rec

    ok = fail = 0
    t_start = time.monotonic()
    results = [None] * len(bursts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(do, b): k for k, b in enumerate(bursts)}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            k = futs[fut]; rec = fut.result(); results[k] = rec
            done += 1
            if 'error' in rec:
                fail += 1
            else:
                ok += 1
            if done % 20 == 0 or 'error' in rec:
                d = rec.get('detection', {})
                poss = d.get('possession', {}) if isinstance(d, dict) else {}
                tag_s = rec.get('error') or (f"phase={d.get('phase')} poss={poss.get('team')}/{poss.get('third')} "
                                             f"#{poss.get('player_shirt_number')} ev={[e.get('type') for e in d.get('events',[])]}")
                print(f"  [{done}/{len(bursts)}] vts={rec['video_time_s']:.1f}s {rec['latency_ms']}ms {tag_s}")

    with out_path.open('w') as fh:
        for rec in results:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f"\nDone in {time.monotonic()-t_start:.0f}s | ok={ok} fail={fail} -> {out_path}")


if __name__ == '__main__':
    main()

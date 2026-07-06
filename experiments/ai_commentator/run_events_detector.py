#!/usr/bin/env python3
"""Run the events_detector_v1 prompt against burst-windows of frames/*.jpg.

Outputs strict-JSON detections per burst to events_v1.jsonl.

Usage:
  # Full sweep (all 545 frames, ~135 bursts every 4th window):
  ./run_events_detector.py

  # Smoke test on N bursts:
  ./run_events_detector.py --limit 5

  # Sparser sampling (stride between burst centres, in frames):
  ./run_events_detector.py --stride 8

  # Choose start burst (useful to target known events):
  ./run_events_detector.py --start 100 --limit 10

  # Alternate model (defaults to gpt-5.5):
  ./run_events_detector.py --model gpt-5.4-mini
"""
from __future__ import annotations
import argparse, base64, json, os, re, sys, time
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
PROMPT_PATH = BASE / 'prompts' / 'events_detector_v1.txt'
OUT_JSONL = BASE / 'events_v1.jsonl'

SAMPLE_INTERVAL_S = 0.55
BURST_SIZE = 4
MAX_OUTPUT_TOKENS = 800


def encode_jpeg(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, model, burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}",
        })
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=model,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning={"effort": "low"},
        )
        return (resp.output_text or '').strip(), int((time.monotonic() - t0) * 1000), None
    except Exception as e:
        return None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {str(e)[:200]}"


JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def extract_json(raw: str):
    if not raw:
        return None, 'empty'
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?', '', raw).rstrip('`').strip()
    m = JSON_RE.search(raw)
    if not m:
        return None, 'no_json_object'
    try:
        return json.loads(m.group(0)), None
    except json.JSONDecodeError as e:
        return None, f'json_decode: {e}'


REQUIRED_TOP = {'phase', 'possession', 'ball_state', 'events'}


def validate_shape(obj):
    if not isinstance(obj, dict):
        return 'not_dict'
    missing = REQUIRED_TOP - obj.keys()
    if missing:
        return f'missing_keys: {sorted(missing)}'
    if not isinstance(obj.get('events'), list):
        return 'events_not_list'
    if not isinstance(obj.get('possession'), dict):
        return 'possession_not_dict'
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='gpt-5.5')
    ap.add_argument('--limit', type=int, default=0, help='0 = all')
    ap.add_argument('--stride', type=int, default=4,
                    help='frames between burst centres (default 4 = one burst per ~2.2s)')
    ap.add_argument('--start', type=int, default=0, help='burst index to start at')
    ap.add_argument('--out', default=str(OUT_JSONL))
    args = ap.parse_args()

    prompt = PROMPT_PATH.read_text()
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    if not frame_paths:
        sys.exit(f"No frames in {FRAMES_DIR}")

    bursts = []
    for i in range(BURST_SIZE - 1, len(frame_paths), args.stride):
        window = frame_paths[i - BURST_SIZE + 1: i + 1]
        video_time_s = (i + 1) * SAMPLE_INTERVAL_S
        bursts.append((i, video_time_s, window))

    bursts = bursts[args.start:]
    if args.limit:
        bursts = bursts[:args.limit]

    print(f"Model: {args.model} | Prompt: {PROMPT_PATH.name} | "
          f"Bursts: {len(bursts)} | Stride: {args.stride} | Out: {args.out}")

    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    out_path = Path(args.out)
    ok = 0
    parse_fail = 0
    api_fail = 0
    t_start = time.monotonic()

    with out_path.open('w') as fh:
        for idx, (frame_i, vts, burst) in enumerate(bursts):
            raw, ms, err = call_vision(client, args.model, burst, prompt)
            record = {
                'burst_i': frame_i,
                'video_time_s': round(vts, 2),
                'frames': [p.name for p in burst],
                'latency_ms': ms,
            }
            if err:
                record['error'] = err
                api_fail += 1
                fh.write(json.dumps(record) + '\n'); fh.flush()
                print(f"  [{idx+1:>3}/{len(bursts)}] burst={frame_i} vts={vts:.1f}s "
                      f"{ms}ms API_FAIL {err}")
                continue

            obj, parse_err = extract_json(raw)
            if parse_err:
                record['raw'] = raw[:500]
                record['error'] = f'parse: {parse_err}'
                parse_fail += 1
                fh.write(json.dumps(record) + '\n'); fh.flush()
                print(f"  [{idx+1:>3}/{len(bursts)}] burst={frame_i} vts={vts:.1f}s "
                      f"{ms}ms PARSE_FAIL {parse_err}")
                continue

            shape_err = validate_shape(obj)
            if shape_err:
                record['raw'] = raw[:500]
                record['detection'] = obj
                record['error'] = f'shape: {shape_err}'
                parse_fail += 1
                fh.write(json.dumps(record) + '\n'); fh.flush()
                print(f"  [{idx+1:>3}/{len(bursts)}] burst={frame_i} vts={vts:.1f}s "
                      f"{ms}ms SHAPE_FAIL {shape_err}")
                continue

            record['detection'] = obj
            fh.write(json.dumps(record) + '\n'); fh.flush()
            ok += 1
            phase = obj.get('phase', '?')
            poss = obj.get('possession', {})
            events = obj.get('events', [])
            ev_types = [e.get('type', '?') for e in events]
            print(f"  [{idx+1:>3}/{len(bursts)}] burst={frame_i} vts={vts:.1f}s "
                  f"{ms}ms OK phase={phase} poss={poss.get('team','?')}/"
                  f"{poss.get('third','?')} events={ev_types}")

    total = time.monotonic() - t_start
    print(f"\nDone in {total:.0f}s | ok={ok} parse_fail={parse_fail} api_fail={api_fail}")


if __name__ == '__main__':
    main()

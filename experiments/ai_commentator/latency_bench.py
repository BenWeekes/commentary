#!/usr/bin/env python3
"""Controlled vision-latency benchmark on an idle box.

Sweeps model tier x frame size x frame count x max_output_tokens over the same
5 bursts, sequentially (no self-contention). Answers: what recipe gets the
detector call fast enough that a live line can land on the play?
"""
import json, statistics, sys
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
import run_events_detector as D
from openai import OpenAI
import os
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

PROMPT = Path('/home/ubuntu/commentary/experiments/ai_commentator/prompts/events_detector_v1.txt').read_text()
B720 = Path('/tmp/live_frames_blend')
B540 = Path('/tmp/lat_bench/540')
B360 = Path('/tmp/lat_bench/360')
BURSTS = [[100,101,102,103],[200,201,202,203],[300,301,302,303],[400,401,402,403],[500,501,502,503]]

def paths(root, idxs, live_fmt):
    if root == B720:
        return [root / f'f_{i:05d}.jpg' for i in idxs]
    return [root / f'f_{i}.jpg' for i in idxs]

CONFIGS = [
    ('gpt-5.6  720p 4f 800tok', 'gpt-5.6', B720, 4, 800),
    ('gpt-5.6  540p 4f 800tok', 'gpt-5.6', B540, 4, 800),
    ('gpt-5.6  540p 4f 300tok', 'gpt-5.6', B540, 4, 300),
    ('gpt-5.6  360p 4f 300tok', 'gpt-5.6', B360, 4, 300),
    ('gpt-5.6  540p 2f 300tok', 'gpt-5.6', B540, 2, 300),
    ('gpt-5.4-mini 540p 4f 800tok', 'gpt-5.4-mini', B540, 4, 800),
    ('gpt-5.4-mini 540p 4f 300tok', 'gpt-5.4-mini', B540, 4, 300),
]

def call(model, burst_paths, max_tok):
    old = D.MAX_OUTPUT_TOKENS
    D.MAX_OUTPUT_TOKENS = max_tok
    try:
        raw, ms, err = D.call_vision(client, model, burst_paths, PROMPT)
    finally:
        D.MAX_OUTPUT_TOKENS = old
    ok = False
    if not err:
        obj, perr = D.extract_json(raw)
        ok = (perr is None) and (D.validate_shape(obj) is None)
    return ms, ok, err

print(f"{'config':32s} {'median':>7s} {'min':>6s} {'max':>6s}  ok/n")
results = {}
for label, model, root, nf, mtok in CONFIGS:
    lats, oks = [], 0
    for idxs in BURSTS:
        bp = paths(root, idxs[-nf:], None)
        ms, ok, err = call(model, bp, mtok)
        lats.append(ms); oks += ok
        if err: print('   err:', err[:80])
    lats.sort()
    results[label] = lats
    print(f"{label:32s} {statistics.median(lats)/1000:6.1f}s {lats[0]/1000:5.1f}s {lats[-1]/1000:5.1f}s  {oks}/{len(BURSTS)}")

Path('/home/ubuntu/commentary/experiments/ai_commentator/latency_bench.json').write_text(json.dumps(results, indent=1))

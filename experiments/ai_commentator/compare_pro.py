#!/usr/bin/env python3
"""Fast batch comparison: gpt-5.5 vs gpt-5.5-pro on the same 20 burst frames.

Runs both models against evenly-spaced burst points from the master frames dir,
uses the same safe_draft prompt as v20's live pipeline. Prints side-by-side
outputs + per-call latency + total wall time.

Judge is skipped here (offline, no need). Rough qualitative comparison.
"""
import base64, json, os, sys, time
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from live_srt_run import SAFE_DRAFT_PROMPT, build_prompt
from run_v5 import build_match_context
from rich_context import build_rich_context_text
from run_v4 import summarise_alias_usage
from run_v5 import format_sub_history, format_pitch_state
from repetition_helpers import summarise_referee_usage

FRAMES_DIR = Path('/home/ubuntu/commentary/experiments/ai_commentator/frames')
frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
print(f"Master frames: {len(frame_paths)}")

# 20 evenly-spaced sample points, each with a 5-frame burst
N_SAMPLES = 20
BURST_SIZE = 5
sample_indices = [int(len(frame_paths) * (i+1) / (N_SAMPLES+1)) for i in range(N_SAMPLES)]
print(f"Sampling bursts at frame indices {sample_indices[:5]}...")

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
ctx = build_match_context()
rich_ctx = build_rich_context_text(ctx)


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_model(burst_paths, prompt, model):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        kwargs = dict(
            model=model,
            input=[{"role": "user", "content": content}],
            max_output_tokens=1500,  # generous — reasoning tokens count against this
        )
        if not model.endswith('-pro'):
            kwargs['reasoning'] = {"effort": "low"}
        resp = client.responses.create(**kwargs)
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000)
    except Exception as e:
        return f"ERR: {str(e)[:150]}", int((time.monotonic()-t0)*1000)


# Import globals live_srt_run needs
import live_srt_run
live_srt_run.PROMPT_STYLE = 'safe_draft'
live_srt_run.MODEL = 'gpt-5.5'

# Build a bare prompt (no previous accepted lines, no sub state)
def bare_prompt():
    return build_prompt(
        rich_ctx=rich_ctx, latest_time_s=100.0, previous=[],
        alias_usage=summarise_alias_usage([], ctx['aliases']),
        sub_hist=format_sub_history([]),
        pitch_state=format_pitch_state(ctx['roster'], []),
        referee_usage=summarise_referee_usage([]),
    )

prompt = bare_prompt()
print(f"prompt size: {len(prompt)} chars\n")

results = []
totals = {'gpt-5.5': 0, 'gpt-5.5-pro': 0}
for i, idx in enumerate(sample_indices):
    start = max(0, idx - BURST_SIZE + 1)
    burst = frame_paths[start:idx+1]
    if len(burst) < BURST_SIZE:
        continue
    video_time_s = (idx + 1) * 0.55
    print(f"[{i+1:2d}/{N_SAMPLES}] frame idx={idx} video_time={video_time_s:.1f}s ({len(burst)} frames):")
    o1, m1 = call_model(burst, prompt, 'gpt-5.5')
    o2, m2 = call_model(burst, prompt, 'gpt-5.5-pro')
    totals['gpt-5.5'] += m1
    totals['gpt-5.5-pro'] += m2
    print(f"  gpt-5.5     ({m1:5d}ms):  {o1[:110]!r}")
    print(f"  gpt-5.5-pro ({m2:5d}ms):  {o2[:110]!r}")
    print()
    results.append({'idx': idx, 'video_time_s': video_time_s,
                    'gpt55_ms': m1, 'gpt55_out': o1,
                    'pro_ms': m2, 'pro_out': o2})

print(f"\n=== Summary over {len(results)} samples ===")
print(f"  gpt-5.5     total {totals['gpt-5.5']/1000:.1f}s  mean/burst {totals['gpt-5.5']/len(results):.0f}ms")
print(f"  gpt-5.5-pro total {totals['gpt-5.5-pro']/1000:.1f}s  mean/burst {totals['gpt-5.5-pro']/len(results):.0f}ms")

out_path = Path('/home/ubuntu/commentary/experiments/ai_commentator/compare_pro.json')
out_path.write_text(json.dumps({'samples': results, 'totals': totals}, indent=2))
print(f"Wrote {out_path}")

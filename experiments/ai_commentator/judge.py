#!/usr/bin/env python3
"""LLM-judge metrics for an AI commentator variant.

For each accepted line in <variant>_tagged.jsonl, ask gpt-5.5 to rate it on:
  - hallucination_likely (0/1) — does this claim something not visibly happening?
  - human_likeness (1-5) — does this sound like a real broadcaster vs a captioner?
  - subject_present (0/1) — is the subject (player/team) at least plausibly in frame?

Also asks: coverage — for each Soniox turn, is there an AI line within ±5 s that
references the same subject? (Soniox gold = the real-broadcaster reference.)

Reports aggregate scores and writes to leaderboard.json.

Usage:
  python judge.py <variant> <ai_jsonl> [--sample N]
"""
import json, sys, os, re, argparse, base64, time
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
LEADERBOARD = BASE / 'leaderboard.json'
GOLD = BASE / 'gold_soniox_5min.jsonl'

JUDGE_MODEL = 'gpt-5.5'
SAMPLE_INTERVAL_S = 0.55  # must match the runner


def frame_for_time_s(t_s):
    """Return path to the frame closest to a given video time (or None)."""
    # frame N has time (N+1)*0.55
    idx = max(0, int(round(t_s / SAMPLE_INTERVAL_S)) - 1)
    p = FRAMES_DIR / f"f_{idx:04d}.jpg"
    return p if p.exists() else None


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


JUDGE_SYSTEM = """You are scoring an AI football-commentator line.

You see a single video frame from the moment the line was about, and the
line itself. Rate the line on three dimensions and return STRICT JSON.

Schema:
{
  "hallucination_likely": 0 or 1,   // 1 if the line claims an event not visibly happening (a sub, a goal, a save, a tackle) — be strict about events that should be obvious from the frame
  "subject_present": 0 or 1,        // 1 if the player/team referenced is plausibly visible in the frame (or it's about the field state in general)
  "human_likeness": 1-5,            // 5 = sounds like a real broadcaster: natural speech rhythm, idiomatic, vivid; 3 = passable; 1 = sounds like a robotic caption
  "rationale": "very brief reason"
}

Return ONLY the JSON, no other text."""


def judge_line(text, frame_path):
    content = [{"type": "input_text", "text": f"Line spoken: \"{text}\"\nFrame attached. Judge per schema."}]
    if frame_path:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(frame_path)}"})
    try:
        resp = client.responses.create(
            model=JUDGE_MODEL,
            instructions=JUDGE_SYSTEM,
            input=[{"role": "user", "content": content}],
            max_output_tokens=400,
            reasoning={"effort": "low"},
        )
        raw = (resp.output_text or '').strip()
        # extract JSON object
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m: return None
        return json.loads(m.group(0))
    except Exception as e:
        print(f"  judge error: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('variant')
    ap.add_argument('jsonl')
    ap.add_argument('--sample', type=int, default=30, help='cap on lines judged')
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.jsonl) if l.strip()]
    # sample uniformly
    if len(rows) > args.sample:
        step = len(rows) / args.sample
        sampled = [rows[int(i * step)] for i in range(args.sample)]
    else:
        sampled = rows
    print(f"Judging {len(sampled)} of {len(rows)} lines for variant '{args.variant}'")

    results = []
    t0 = time.time()
    for i, r in enumerate(sampled):
        text = r.get('text') or r.get('fr') or ''
        t_s = r.get('video_time_s') or r.get('start_s') or 0
        frame = frame_for_time_s(t_s)
        verdict = judge_line(text, frame)
        if verdict:
            verdict['_text'] = text[:80]
            verdict['_t'] = t_s
            results.append(verdict)
        if i % 5 == 0:
            print(f"  [{i:2d}/{len(sampled)}] {time.time()-t0:.0f}s {text[:70]!r}  → {verdict}")

    # Aggregate
    n = len(results)
    if n == 0:
        print("No verdicts."); return
    halluc = sum(r.get('hallucination_likely', 0) for r in results) / n
    subj = sum(r.get('subject_present', 0) for r in results) / n
    hl = sum(r.get('human_likeness', 0) for r in results) / n

    # Coverage: how many Soniox turns have a corresponding AI line within ±5s
    gold = [json.loads(l) for l in open(GOLD)]
    ai_times = [r.get('video_time_s') or r.get('start_s') or 0 for r in rows]
    covered = 0
    for g in gold:
        gt = g['start_s']
        if any(abs(t - gt) <= 5 for t in ai_times):
            covered += 1
    coverage = covered / max(1, len(gold))

    summary = {
        'judge_sample_n': n,
        'judge_hallucination_rate': round(halluc, 3),
        'judge_subject_present_rate': round(subj, 3),
        'judge_human_likeness_mean': round(hl, 2),
        'soniox_turn_coverage': round(coverage, 3),
    }
    print(f"\nJudge summary for {args.variant}:")
    print(json.dumps(summary, indent=2))

    # Merge into leaderboard
    board = json.load(open(LEADERBOARD)) if LEADERBOARD.exists() else {'variants': []}
    updated = False
    for v in board['variants']:
        if v['variant'] == args.variant:
            v.update(summary)
            updated = True
            break
    if not updated:
        board['variants'].append({'variant': args.variant, **summary})
    json.dump(board, open(LEADERBOARD, 'w'), indent=2)

    # Also save raw verdicts
    raw_path = BASE / f"judge_{args.variant}.json"
    json.dump({'summary': summary, 'verdicts': results}, open(raw_path, 'w'), indent=2)
    print(f"Updated leaderboard; raw verdicts at {raw_path}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Tier A milestone 2 — inject the YOLOv8 tracking layer into the gpt-5.5
playerist prompt and A/B it against the same pipeline with tracking OFF.

Uses tracking_tier_a.json (from track_tier_a.py). The TRACKING block gives the
model high-confidence SPATIAL facts for the newest frame: is it a live pitch
view or a replay/close-up (from green_ratio), where the ball is, and rough team
presence. No jersey identities yet (that's milestone 4 / OCR).

Usage:
  python run_gpt55_track.py on     # tracking injected  -> commentary_gpt55_track_on.jsonl
  python run_gpt55_track.py off    # identical pipeline, no tracking -> ..._off.jsonl
"""
from __future__ import annotations
import base64, json, os, re, sys, time
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v5 import (
    is_repetitive_trigram, detect_sub, format_sub_history, format_pitch_state,
    cheap_tag_guess, gate_for_tag, GATE_NORMAL_S,
    build_match_context, build_match_context_text, is_no_call,
)
from run_v4 import summarise_alias_usage
from run_gpt55_variant import VARIANT_PROMPTS, build_prompt, encode_jpeg

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
TRACK_JSON = BASE / 'tracking_tier_a.json'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.5'
MAX_OUTPUT_TOKENS = 500
NATURAL_LAG_S = 0.3

TEAM_MAP = {'red': 'Mainz', 'yellow': 'Union', 'white/light': 'Union', 'blue': 'Union'}


def load_tracking():
    if not TRACK_JSON.exists():
        print(f"WARNING: {TRACK_JSON} missing — tracking will be empty"); return {}, {}
    d = json.load(open(TRACK_JSON))
    teams = d['meta']['teams']  # {'team0':'red','team1':'yellow'}
    return {r['frame']: r for r in d['frames']}, teams


def format_tracking(rec, teams):
    """One compact TRACKING block for the newest frame (or None if no rec)."""
    if not rec:
        return None
    green = rec.get('green_ratio', 0)
    live = green >= 0.35
    lines = ["TRACKING (external detector — high-confidence facts for the NEWEST frame):"]
    if live:
        lines.append(f"- view: LIVE pitch shot (grass {green:.0%})")
    else:
        lines.append(f"- view: likely REPLAY or tight close-up (grass only {green:.0%}) "
                     f"— do NOT narrate a fresh live action here; keep it brief/generic")
    b = rec.get('ball')
    if b:
        lines.append(f"- ball: detected in the {b['zone']} of frame (conf {b['conf']})")
    else:
        lines.append("- ball: not detected this frame")
    # team presence among on-pitch players + side skew
    onp = [p for p in rec.get('players', []) if p.get('on_pitch')]
    def team_name(t):
        return TEAM_MAP.get(teams.get(f'team{t}', ''), f'team{t}')
    cnt = {0: 0, 1: 0}
    thirds = {'left': 0, 'mid': 0, 'right': 0}
    for p in onp:
        if p.get('team') in (0, 1):
            cnt[p['team']] += 1
        cx = (p['bbox'][0] + p['bbox'][2]) / 2 / 960
        thirds['left' if cx < 0.34 else ('right' if cx > 0.66 else 'mid')] += 1
    if onp:
        skew = max(thirds, key=thirds.get)
        lines.append(f"- players on pitch: {cnt[0]} {team_name(0)}, {cnt[1]} {team_name(1)}; "
                     f"bodies concentrated {skew} of frame")
    lines.append("Treat these as ground truth. Do not claim an event inconsistent with them.")
    return "\n".join(lines)


def call_vision(client, burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS, reasoning={"effort": "low"})
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"{type(e).__name__}: {str(e)[:200]}"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'on'
    assert mode in ('on', 'off')
    tracking_on = (mode == 'on')
    OUT = BASE / f'commentary_gpt55_track_{mode}.jsonl'
    OUT_SCHED = BASE / f'commentary_gpt55_track_{mode}_scheduled.jsonl'

    track_by_frame, teams = load_tracking()
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context(); ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']; roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"mode={mode} tracking_on={tracking_on} teams={teams} frames={len(frame_paths)}")
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1: i + 1]
        newest = current[-1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S,
                       ([prev_last] + current) if prev_last else current, newest.name))

    accepted = []; all_attempts = []; subs = []; booth_busy_until = 0.0
    no_call = repetitive = errors = skipped = 0
    t_start = time.time(); last_print = time.time()
    for bi, (i, latest_time_s, burst, newest_name) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs); pitch_state = format_pitch_state(ctx['roster'], subs)
        prompt = build_prompt(ctx_text, latest_time_s, prev_texts, alias_usage,
                              sub_hist, pitch_state, 'playerist')
        if tracking_on:
            tb = format_tracking(track_by_frame.get(newest_name), teams)
            if tb:
                prompt = prompt.replace("Produce your next call (or NO_CALL):",
                                        tb + "\n\nProduce your next call (or NO_CALL):")
        text, vision_ms, err = call_vision(client, burst, prompt)
        att = {'burst_index': i, 'video_time_s': round(latest_time_s, 2),
               'vision_latency_ms': vision_ms, 'text': text, 'accepted': False,
               'reason': None, 'error': err, 'newest_frame': newest_name}
        if err:
            errors += 1; att['reason'] = 'error'
        elif not text:
            att['reason'] = 'empty'
        elif is_no_call(text):
            no_call += 1; att['reason'] = 'no_call'
        elif is_repetitive_trigram(text, [a['text'] for a in accepted], last_n=5):
            repetitive += 1; att['reason'] = 'trigram_dup'
        else:
            sub = detect_sub(text, roster_by_short)
            if sub:
                if any(s['off'] == sub[0] and s['on'] == sub[1] for s in subs):
                    repetitive += 1; att['reason'] = 'dup_sub'; all_attempts.append(att); continue
                on_pitch = {p['short_name'] for p in ctx['roster'] if p['role'] == 'starter'}
                for s in subs:
                    on_pitch.discard(s['off']); on_pitch.add(s['on'])
                if sub[0] not in on_pitch or sub[1] in on_pitch:
                    att['reason'] = 'sub_invalid'; all_attempts.append(att); continue
                subs.append({'off': sub[0], 'on': sub[1], 'at_s': round(latest_time_s, 1)})
            est_tag = cheap_tag_guess(text); gate = gate_for_tag(est_tag)
            est_duration_s = max(1.2, len(text.split()) / 3.0)
            sched_start = latest_time_s + NATURAL_LAG_S
            sched_end = sched_start + est_duration_s + (gate - GATE_NORMAL_S)
            att.update({'accepted': True, 'est_tag': est_tag,
                        'scheduled_start_s': round(sched_start, 3),
                        'scheduled_end_s': round(sched_end, 3)})
            accepted.append(att); booth_busy_until = sched_end
        all_attempts.append(att)
        if time.time() - last_print > 15:
            print(f"  burst {bi}/{len(bursts)} t={latest_time_s:.0f}s accepted={len(accepted)} "
                  f"skipped={skipped} elapsed={time.time()-t_start:.0f}s")
            last_print = time.time()

    print(f"\nSummary [track_{mode}]: accepted={len(accepted)} skipped={skipped} "
          f"no_call={no_call} rep={repetitive} err={errors} subs={len(subs)} wall={time.time()-t_start:.0f}s")
    with open(OUT, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    print(f"wrote {OUT} + {OUT_SCHED}")


if __name__ == '__main__':
    main()

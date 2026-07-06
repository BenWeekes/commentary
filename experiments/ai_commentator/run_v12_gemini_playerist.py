#!/usr/bin/env python3
"""v12 = single Gemini vision + playerist prompt + rich context.

The one prompt-model combo we never tested. Fills the matrix.
"""
from __future__ import annotations
import base64, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,_,v=line.partition('='); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ['GEMINI_API_KEY'] = os.environ.get('GEMINI_API_KEY', '')

sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v4 import summarise_alias_usage
from run_v5 import (
    is_repetitive_trigram, detect_sub, format_sub_history, format_pitch_state,
    cheap_tag_guess, gate_for_tag, GATE_NORMAL_S,
    build_match_context, is_no_call,
)
from rich_context import build_rich_context_text
from run_v10_single_playerist import PLAYERIST_PROMPT, build_prompt

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v12.jsonl'
OUT_KEPT = BASE / 'commentary_v12_kept.txt'
OUT_SCHED = BASE / 'commentary_v12_scheduled.jsonl'

GEMINI_MODEL = 'gemini-2.5-flash'
GEMINI_KEY = os.environ['GEMINI_API_KEY']
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def gemini_call(burst_paths, prompt):
    parts = [{"text": prompt}]
    for p in burst_paths:
        parts.append({"inline_data": {"mime_type":"image/jpeg","data": encode_jpeg(p)}})
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.5, "maxOutputTokens": 100,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                 headers={'Content-Type': 'application/json'})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        parts_out = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        text = parts_out[0].get('text', '').strip() if parts_out else ''
        return text, int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {str(e)[:200]}"


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"v12: single {GEMINI_MODEL} + playerist + rich context")

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S,
                       ([prev_last] + current) if prev_last else current))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []; booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0
    t_start = time.time(); last_print = time.time()

    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-12:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs)
        pitch_state = format_pitch_state(ctx['roster'], subs)
        prompt = build_prompt(rich_ctx, latest_time_s, prev_texts,
                              alias_usage, sub_hist, pitch_state)
        text, vision_ms, err = gemini_call(burst, prompt)
        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': vision_ms, 'text': text,
            'accepted': False, 'reason': None, 'error': err,
        }
        if err:
            errors += 1; attempt['reason']='error'
        elif not text:
            attempt['reason']='empty'
        elif is_no_call(text):
            no_call += 1; attempt['reason']='no_call'
        elif is_repetitive_trigram(text, [a['text'] for a in accepted], last_n=5):
            repetitive += 1; attempt['reason']='trigram_dup'
        else:
            sub = detect_sub(text, roster_by_short)
            if sub:
                if any(s['off']==sub[0] and s['on']==sub[1] for s in subs):
                    repetitive += 1; attempt['reason']='dup_sub'
                    all_attempts.append(attempt); continue
                on_pitch = {p['short_name'] for p in ctx['roster'] if p['role']=='starter'}
                for s in subs:
                    on_pitch.discard(s['off']); on_pitch.add(s['on'])
                if sub[0] not in on_pitch:
                    attempt['reason'] = f'sub_off_not_on_pitch({sub[0]})'
                    all_attempts.append(attempt); continue
                if sub[1] in on_pitch:
                    attempt['reason'] = f'sub_on_already_on_pitch({sub[1]})'
                    all_attempts.append(attempt); continue
                subs.append({'off': sub[0], 'on': sub[1], 'at_s': round(latest_time_s, 1)})
            est_tag = cheap_tag_guess(text)
            gate = gate_for_tag(est_tag)
            words = len(text.split())
            est_duration_s = max(1.2, words / 3.0)
            scheduled_start_s = latest_time_s + NATURAL_LAG_S
            scheduled_end_s = scheduled_start_s + est_duration_s + (gate - GATE_NORMAL_S)
            attempt.update({
                'accepted': True, 'est_tag': est_tag,
                'est_duration_s': round(est_duration_s, 3),
                'scheduled_start_s': round(scheduled_start_s, 3),
                'scheduled_end_s': round(scheduled_end_s, 3),
                'sub_detected': sub,
            })
            accepted.append(attempt)
            booth_busy_until = scheduled_end_s
        all_attempts.append(attempt)
        if time.time() - last_print > 15:
            print(f"  b{burst_idx}/{len(bursts)} t={latest_time_s:.1f}s acc={len(accepted)} el={time.time()-t_start:.0f}s last={(text or '')[:60]!r}")
            last_print = time.time()

    print(f"\nSummary: attempts={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors}")
    if accepted:
        lats = sorted(a['vision_latency_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision p50={pct(lats,0.5)}ms p90={pct(lats,0.9)}ms")
    print(f"Wall: {time.time()-t_start:.0f}s")
    print(f"Subs: {subs}")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# v12: single {GEMINI_MODEL} + playerist + rich context (fills matrix)\n")
        f.write(f"# {len(accepted)} accepted; subs {subs}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")


if __name__ == '__main__':
    main()

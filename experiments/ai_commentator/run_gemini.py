#!/usr/bin/env python3
"""AI commentator — Gemini vision parallel.

Same 4-frame burst pipeline as v5 (sub memory, trigram dedup, frame carry-over,
dynamic gate, alias rotation, scoreline rule, all 5 v4 rules). Vision call
swapped to Gemini 2.5 Flash.

Outputs commentary_gemini.jsonl + commentary_gemini_scheduled.jsonl.
"""
from __future__ import annotations
import base64, json, os, re, sys, time
import urllib.request, urllib.error
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ['GEMINI_API_KEY'] = os.environ.get('GEMINI_API_KEY', '')

sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v4 import (
    _parse_roster_text, _load_sr_positions, _load_team_aliases,
    _format_roster_block, _format_alias_block,
    PLAYER_INSIGHTS, MANAGER_INSIGHTS, STYLE_EXAMPLES,
    summarise_alias_usage,
)
from run_v5 import (
    trigrams, is_repetitive_trigram, detect_sub, format_sub_history,
    format_pitch_state, build_visual_prompt, cheap_tag_guess,
    gate_for_tag, GATE_NORMAL_S, build_match_context, build_match_context_text,
    is_no_call,
)

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_gemini.jsonl'
OUT_KEPT = BASE / 'commentary_gemini_kept.txt'
OUT_SCHED = BASE / 'commentary_gemini_scheduled.jsonl'

GEMINI_MODEL = 'gemini-2.5-flash'
GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
SR_TTS = 16000
DURATION_S = 300.0
NATURAL_LAG_S = 0.3


def gemini_call(prompt, image_paths):
    parts = [{"text": prompt}]
    for p in image_paths:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(p.read_bytes()).decode('ascii'),
            }
        })
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.55,
            "maxOutputTokens": 80,
            "thinkingConfig": {"thinkingBudget": 0},
        }
    }).encode()
    req = urllib.request.Request(
        GEMINI_URL, data=body,
        headers={'Content-Type': 'application/json'}
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        # Extract response text
        cands = data.get('candidates', [])
        if not cands:
            return None, int((time.monotonic()-t0)*1000), f"no_candidates: {data}"
        parts_out = cands[0].get('content', {}).get('parts', [])
        if not parts_out:
            return None, int((time.monotonic()-t0)*1000), f"no_parts: {data}"
        text = parts_out[0].get('text', '').strip()
        return text, int((time.monotonic()-t0)*1000), None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:200]
        return None, int((time.monotonic()-t0)*1000), f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"{type(e).__name__}: {str(e)[:200]}"


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Frames: {len(frame_paths)}")
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"Context size: {len(ctx_text)} chars; aliases: {bool(aliases)}; roster: {len(roster_by_short)}")
    print(f"Model: {GEMINI_MODEL}")

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        burst_frames = ([prev_last] + current) if prev_last else current
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, burst_frames))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []
    booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0

    t_start = time.time()
    last_print = time.time()
    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1
            continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_history_text = format_sub_history(subs)
        pitch_state_text = format_pitch_state(ctx['roster'], subs)

        prompt = build_visual_prompt(ctx_text, latest_time_s, prev_texts,
                                     alias_usage, sub_history_text, pitch_state_text)
        text, vision_ms, err = gemini_call(prompt, burst)
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
                already = any(s['off'] == sub[0] and s['on'] == sub[1] for s in subs)
                if already:
                    repetitive += 1; attempt['reason']='dup_sub_announce'
                    all_attempts.append(attempt); continue
                on_pitch = {p['short_name'] for p in ctx['roster'] if p['role'] == 'starter'}
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
                'accepted': True, 'est_tag': est_tag, 'gate_s': gate,
                'est_duration_s': round(est_duration_s, 3),
                'scheduled_start_s': round(scheduled_start_s, 3),
                'scheduled_end_s': round(scheduled_end_s, 3),
                'sub_detected': sub,
            })
            accepted.append(attempt)
            booth_busy_until = scheduled_end_s
        all_attempts.append(attempt)
        if time.time() - last_print > 10:
            print(f"  burst {burst_idx}/{len(bursts)} t={latest_time_s:.1f}s accepted={len(accepted)} skipped={skipped} subs={len(subs)} elapsed={time.time()-t_start:.0f}s last={(text or '')[:60]!r}")
            last_print = time.time()

    print(f"\nGemini summary: vision={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep/dup={repetitive} err={errors} subs={len(subs)}")
    if accepted:
        lats_v = sorted(a['vision_latency_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision p50={pct(lats_v,0.5)}ms p90={pct(lats_v,0.9)}ms")
    print(f"Wall time: {time.time()-t_start:.0f}s")
    print(f"Subs detected: {subs}")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts:
            f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted:
            f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# AI commentary GEMINI — {GEMINI_MODEL}, same pipeline as v5\n")
        f.write(f"# {len(accepted)} accepted of {len(all_attempts)} calls\n")
        f.write(f"# Subs tracked: {subs}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

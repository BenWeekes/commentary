#!/usr/bin/env python3
"""v10 = single-model playerist control.

Same pipeline as v5 (single gpt-5.4-mini vision call per burst, no Gemini, no
arbiter, no verifier) but with the "playerist" prompt that worked best for
gpt-5.5 (banning explicit team-name references in favour of naming a player).

Purpose: baseline test — does the playerist prompt alone reduce hallucinations
on gpt-5.4-mini? If yes, hybrid isn't adding value.

Latency: same as v5 (~1.7s vision + 0.6s TTS+beat) → pipeline p90 ~3s.
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
from run_v4 import summarise_alias_usage
from run_v5 import (
    is_repetitive_trigram, detect_sub, format_sub_history, format_pitch_state,
    cheap_tag_guess, gate_for_tag, GATE_NORMAL_S,
    build_match_context, is_no_call,
)
from rich_context import build_rich_context_text

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v10.jsonl'
OUT_KEPT = BASE / 'commentary_v10_kept.txt'
OUT_SCHED = BASE / 'commentary_v10_scheduled.jsonl'

MODEL = 'gpt-5.4-mini'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


PLAYERIST_PROMPT = """You are a live English football commentator on a
Bundesliga broadcast.

You see a burst of frames (oldest first, last one is NEWEST). Comment on the
NEWEST frame. First frame is carry-over — use it for continuity.

OUTPUT — player-first, 5-12 words:
- ALMOST EVERY line: [player name] + [generic verb / role] + [optional location]
  Example: "Amiri drops back into midfield." "Klaus rolls it out."
- ALMOST NEVER say "Mainz" / "Union" / any team alias. Naming a player already
  tells the viewer their team.
- A team alias is only allowed for: (a) an announced substitution, (b) a
  tactical shape observation involving the whole team, (c) a restart where
  the player is genuinely unidentifiable.
- Do NOT claim specific events (save, tackle, shot, foul, sub, card) UNLESS
  the event is UNAMBIGUOUSLY mid-execution in the newest frame.
- Occasional player misidentification is acceptable — better than "the reds"
  every line.
- If nothing meaningful is happening (routine possession, replay, static,
  crowd shot), return NO_CALL.

HARD RULES:
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.
- DO NOT state the scoreline.
- Sub-board (4th official electronic panel): RED top = off, GREEN bottom = on.
  DO NOT re-announce a sub already listed in "SUBS ALREADY ANNOUNCED".
- GENERIC OVER INCORRECT: if you're guessing about a specific event, describe
  what's visibly there instead (player + generic action).
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def build_prompt(rich_ctx, latest_time_s, previous_calls, alias_usage,
                 sub_hist, pitch_state):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-12:]) or "  - none"
    return f"""{PLAYERIST_PROMPT}

VIDEO CLOCK: {latest_time_s:.1f}s

SUBS ALREADY ANNOUNCED (do not repeat):
{sub_hist}

PITCH STATE:
{pitch_state or "(no subs yet)"}

RECENT ACCEPTED LINES:
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

RICH PRE-GAME CONTEXT
{rich_ctx}

Produce your next call (or NO_CALL):"""


def call_vision(burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=80,
            temperature=0.5,
        )
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"v10: single {MODEL} + playerist + rich context (no hybrid)")

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
    t_start = time.time()
    last_print = time.time()

    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-12:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs)
        pitch_state = format_pitch_state(ctx['roster'], subs)
        prompt = build_prompt(rich_ctx, latest_time_s, prev_texts,
                              alias_usage, sub_hist, pitch_state)
        text, vision_ms, err = call_vision(burst, prompt)
        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': vision_ms, 'text': text,
            'accepted': False, 'reason': None, 'error': err,
        }
        if err:
            errors += 1; attempt['reason'] = 'error'
        elif not text:
            attempt['reason'] = 'empty'
        elif is_no_call(text):
            no_call += 1; attempt['reason'] = 'no_call'
        elif is_repetitive_trigram(text, [a['text'] for a in accepted], last_n=5):
            repetitive += 1; attempt['reason'] = 'trigram_dup'
        else:
            sub = detect_sub(text, roster_by_short)
            if sub:
                if any(s['off']==sub[0] and s['on']==sub[1] for s in subs):
                    repetitive += 1; attempt['reason'] = 'dup_sub'
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
        f.write(f"# v10: single {MODEL} + playerist + rich context (control)\n")
        f.write(f"# {len(accepted)} accepted; subs {subs}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")


if __name__ == '__main__':
    main()

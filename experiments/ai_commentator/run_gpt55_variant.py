#!/usr/bin/env python3
"""gpt-5.5 prompt-variants for the commentator track.

Usage:
  python run_gpt55_variant.py <variant>
  variants:
    quiet     — aggressive NO_CALL targeting real-broadcaster cadence (~8 s gap)
    long      — longer sentences (1-2 sentences per call, 12-16 words)
    playerist — ban explicit team names except sub/restart events

Outputs commentary_gpt55_<variant>.jsonl and ..._scheduled.jsonl.
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
from run_v4 import (
    _format_roster_block, _format_alias_block,
    MANAGER_INSIGHTS, STYLE_EXAMPLES,
    summarise_alias_usage,
)
from run_v5 import (
    is_repetitive_trigram, detect_sub, format_sub_history, format_pitch_state,
    cheap_tag_guess, gate_for_tag, GATE_NORMAL_S,
    build_match_context, build_match_context_text, is_no_call,
)

VARIANT_PROMPTS = {
    'quiet': """OUTPUT — BE QUIET MOST OF THE TIME
- A SKILLED COMMENTATOR IS SILENT for 60-75% of bursts. Return NO_CALL by default.
- Speak ONLY when something genuinely happens:
    * shot, save, header, key tackle
    * substitution, card, foul whistle
    * named player makes a decisive action (a clean break, a pass into the box)
    * something that would be on the highlight reel
- Routine possession, midfield circulation, players standing around → NO_CALL.
- Aim for ~30-40 spoken lines across the whole 5-minute slice (matching the
  real-broadcaster cadence of ~8 s average gap).
- When you DO speak, be specific. No padding, no atmospheric filler.""",

    'long': """OUTPUT — LONGER, MORE NATURAL SENTENCES
- 10-20 words, often 2 short sentences. Match the rhythm of real radio/TV
  commentary, not staccato fragments.
- Example: "Klaus comes well off his line to claim that one — calmly done by
  the goalkeeper, and Union can build again."
- NOT: "Klaus claims the cross."
- A short fragment is fine for a sudden moment ("It's just wide!") but the
  default is a complete spoken sentence with a clause.
- Still return NO_CALL when nothing's happening.""",

    'playerist': """OUTPUT — NAME PLAYERS, NOT TEAMS
- ALMOST NEVER say "Mainz" or "Union" or any team alias. Naming a player
  already implies the team to the viewer.
- Example: "Burke driving wide" — viewer knows Union has the ball.
- Use a team alias ONLY when (a) announcing a substitution; (b) describing
  a tactical shape change involving the whole team; (c) on a restart where
  the player taking it is genuinely unidentifiable.
- For every other line, lead with a player name + verb.
- Still concise (5-12 words) and still NO_CALL when nothing's happening.""",
}

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.5'
MAX_OUTPUT_TOKENS = 500
NATURAL_LAG_S = 0.3


def build_prompt(ctx_text, latest_time_s, previous_calls, alias_usage,
                 sub_hist, pitch_state, variant):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none yet"
    examples = "\n".join(f"  - \"{e}\"" for e in STYLE_EXAMPLES)
    variant_block = VARIANT_PROMPTS[variant]
    return f"""You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers.

PROFILE: experienced English-language sportscaster.

VIDEO CONTEXT
Current video clock: {latest_time_s:.1f}s. The FIRST frame is the last frame
from the PREVIOUS burst — use it for continuity. Comment on the NEWEST frame.

{variant_block}

NAMING — LEAN INTO IT
- Use shirt numbers visible on the back. Cross-reference with the roster.
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.

SUBSTITUTIONS — HARD RULES
1. SUBS ALREADY ANNOUNCED THIS HALF:
{sub_hist}
   DO NOT re-announce. If you see the same fourth-official board again, it
   is the replay of a sub you covered.
2. {pitch_state or "(no subs yet)"}
   When you see the fourth-official electronic board (RED top = off,
   GREEN bottom = on), state as a substitution naming BOTH players via
   roster — only if not in the list above.

SCORELINE RULE
- DO NOT state the scoreline unless score just changed this burst, or
  inside the final 5 min for tension.

SET-PIECE TEAM ATTRIBUTION
- Throw / FK / corner: only name the team when you can clearly see which
  side stands over it. Cross-check shirt number.
- Otherwise describe without naming a team.

STYLE EXAMPLES (illustrative; do NOT copy verbatim):
{examples}

CONTEXT
{ctx_text}

RECENT CALLS — do not repeat the same observation:
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

Produce your next call (or NO_CALL):
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning={"effort": "low"},
        )
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"{type(e).__name__}: {str(e)[:200]}"


def main():
    variant = sys.argv[1]
    assert variant in VARIANT_PROMPTS, f"variant must be one of {list(VARIANT_PROMPTS)}"
    OUT_JSONL = BASE / f'commentary_gpt55_{variant}.jsonl'
    OUT_KEPT = BASE / f'commentary_gpt55_{variant}_kept.txt'
    OUT_SCHED = BASE / f'commentary_gpt55_{variant}_scheduled.jsonl'

    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"VARIANT={variant} | Model: {MODEL} | Frames: {len(frame_paths)}")
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, ([prev_last] + current) if prev_last else current))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []; booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0
    t_start = time.time()
    last_print = time.time()
    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs)
        pitch_state = format_pitch_state(ctx['roster'], subs)
        prompt = build_prompt(ctx_text, latest_time_s, prev_texts, alias_usage,
                              sub_hist, pitch_state, variant)
        text, vision_ms, err = call_vision(client, burst, prompt)
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
                    repetitive += 1; attempt['reason']='dup_sub_announce'
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
            attempt.update({'accepted': True, 'est_tag': est_tag,
                            'est_duration_s': round(est_duration_s,3),
                            'scheduled_start_s': round(scheduled_start_s,3),
                            'scheduled_end_s': round(scheduled_end_s,3),
                            'sub_detected': sub})
            accepted.append(attempt)
            booth_busy_until = scheduled_end_s
        all_attempts.append(attempt)
        if time.time() - last_print > 15:
            print(f"  burst {burst_idx}/{len(bursts)} t={latest_time_s:.1f}s accepted={len(accepted)} skipped={skipped} subs={len(subs)} elapsed={time.time()-t_start:.0f}s last={(text or '')[:60]!r}")
            last_print = time.time()
    print(f"\nSummary [{variant}]: accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors} subs={len(subs)}")
    print(f"Wall: {time.time()-t_start:.0f}s")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# gpt-5.5 {variant} variant\n# {len(accepted)} accepted of {len(all_attempts)} calls\n# Subs: {subs}\n\n")
        for a in accepted: f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

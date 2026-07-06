#!/usr/bin/env python3
"""AI commentator v8 — dual vision + arbiter/judge with frame + rich context + FR.

Architecture:
  Vision A (gpt-5.4-mini) ─┐
                           ├── parallel, each returns {specific, safe, confidence}
  Vision B (Gemini)     ───┘
                           │
             Arbiter/judge (gpt-5.4-mini WITH the newest frame)
                           │  reads: both outputs + frame + running state + rich pre-game
                           │  outputs: {choice, en, fr, quality, reason}
                           │  choice constrained to A.spec/A.safe/B.spec/B.safe/NO_CALL
                           ▼
                        picked line ── TTS (EN) — schedule ═► publish
                                    ── TTS (FR) — schedule ═► publish

Latency target ≤ 5 s p90 pipeline. Rich context loaded from rich_context.py.
"""
from __future__ import annotations
import base64, json, os, re, sys, time, urllib.request, urllib.error
import concurrent.futures
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ['GEMINI_API_KEY'] = os.environ.get('GEMINI_API_KEY', '')

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
OUT_JSONL = BASE / 'commentary_v8.jsonl'
OUT_KEPT = BASE / 'commentary_v8_kept.txt'
OUT_SCHED = BASE / 'commentary_v8_scheduled.jsonl'

OAI_MODEL = 'gpt-5.4-mini'
GEMINI_MODEL = 'gemini-2.5-flash'
ARBITER_MODEL = 'gpt-5.4-mini'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3

GEMINI_KEY = os.environ['GEMINI_API_KEY']
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
)
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


VISION_PROMPT_HEAD = """You are a live English football commentator on a Bundesliga broadcast.

You see a burst of frames (oldest first, last one is NEWEST). Comment on
the NEWEST frame. The first frame is carry-over from the previous burst — use
it for continuity.

Produce TWO variants + a confidence level, as JSON:

  "specific" — 5-14 words. Detailed, may weave in a PRE-GAME insight from the
               context if a named player has a matching note. Use event verbs
               (save, tackle, shot, breakaway, cross) ONLY if the event is
               clearly mid-execution in the NEWEST frame. When uncertain,
               fall back on subject + kit-colour + position + generic tempo.

  "safe"     — 5-10 words. Generic but INFORMATIVE description. Who has the
               ball, where, and roughly what's about to happen or what phase
               of play it is. NO specific outcomes ("saves!", "buries it!")
               UNLESS the event is unambiguously mid-execution.
               Vary phrasing — do NOT just say "X on the ball" every time.
               This is the fallback when your specific and the other AI's
               specific disagree.

  "confidence" — "high" | "medium" | "low"
     high   = shirt number legible; action clearly identifiable
     medium = position + kit colour → player identity is a strong guess
     low    = uncertain who or what

If nothing is worth commenting on (routine possession, replay, static image,
crowd shot), output:
  {"specific": null, "safe": null, "confidence": "no_call"}

Otherwise:
  {"specific": "...", "safe": "...", "confidence": "high|medium|low"}

Return ONLY valid JSON.

HARD RULES:
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.
- DO NOT state the scoreline (it's on-screen).
- Sub-board (fourth-official electronic panel): RED top = off, GREEN bottom = on.
  DO NOT re-announce a sub already listed in "SUBS ALREADY ANNOUNCED" below.
- Rotate team-name aliases; the same short name twice in a row is repetitive.
- GENERIC OVER INCORRECT: if the specific claim would be a guess, downgrade to
  a description that's verifiable from the frame.
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def build_vision_prompt(rich_ctx, latest_time_s, previous_calls, alias_usage,
                        sub_hist, pitch_state):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-12:]) or "  - none"
    return f"""{VISION_PROMPT_HEAD}

VIDEO CLOCK
  {latest_time_s:.1f}s into the match slice.

SUBS ALREADY ANNOUNCED (do not repeat):
{sub_hist}

PITCH STATE:
{pitch_state or "(no subs yet — starting XIs unchanged)"}

RECENT ACCEPTED LINES (do not repeat topics or phrasings):
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

RICH PRE-GAME CONTEXT
{rich_ctx}

Produce JSON:"""


def openai_vision_call(burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=OAI_MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=250,
            temperature=0.5,
        )
        out = (resp.output_text or '').strip()
        return parse_json_response(out), int((time.monotonic()-t0)*1000), out
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"


def gemini_vision_call(burst_paths, prompt):
    parts = [{"text": prompt}]
    for p in burst_paths:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": encode_jpeg(p)}})
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.5, "maxOutputTokens": 250,
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
        },
    }).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                 headers={'Content-Type': 'application/json'})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        parts_out = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        text = parts_out[0].get('text', '') if parts_out else ''
        return parse_json_response(text), int((time.monotonic()-t0)*1000), text
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:200]
        return None, int((time.monotonic()-t0)*1000), f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"


def parse_json_response(text):
    if not text: return None
    m = re.search(r'\{.*?\}', text, re.DOTALL)
    if not m: return None
    try:
        obj = json.loads(m.group(0))
        return {
            'specific': obj.get('specific') or None,
            'safe': obj.get('safe') or None,
            'confidence': obj.get('confidence') or 'medium',
        }
    except json.JSONDecodeError:
        return None


# ---- arbiter / judge ------------------------------------------------------

ARBITER_PROMPT_HEAD = """You are the ARBITER + JUDGE for a live football
commentary AI. You get:
  - The newest video frame the commentators just spoke about
  - Two candidate outputs from two independent vision LLMs, each with a
    "specific" (detailed) and "safe" (generic) variant + confidence
  - The running state (recent accepted lines, sub history)

Your job:
  1. Pick the BEST candidate to speak, or NO_CALL
  2. Constrained choice — you MUST pick VERBATIM one of:
        "A_specific" | "A_safe" | "B_specific" | "B_safe" | "NO_CALL"
     Do NOT rewrite the text.
  3. Verify the frame — reject specifics that claim events not visibly
     happening in the frame; prefer the safer variant when a specific
     appears speculative.
  4. Score the picked line 1-5 for how good live commentary this is:
        5 = human-broadcaster quality
        4 = good, specific + accurate
        3 = passable, mildly generic
        2 = weak / repetitive / low information
        1 = bad — hallucinated event OR nonsense
     Give 0 for NO_CALL.
  5. Translate the picked line into natural French sports commentary (idiomatic,
     concise; preserve proper nouns). Leave empty for NO_CALL.
  6. One-sentence reason for the choice.

Return JSON ONLY:
  {
    "choice": "A_specific|A_safe|B_specific|B_safe|NO_CALL",
    "en": "<verbatim text of the chosen option, empty if NO_CALL>",
    "fr": "<French translation, empty if NO_CALL>",
    "quality": 0-5,
    "reason": "<one short sentence>"
  }

DECISION HINTS:
- If A_specific and B_specific name the same player AND the frame supports it,
  prefer the shorter of the two specifics.
- If they name DIFFERENT players / events, prefer the SAFE variant of whichever
  looks more accurate against the frame.
- If NEITHER specific is plausible, use the SAFE variant.
- If both candidates look like guesses about non-visible events, NO_CALL.
- Downweight lines that repeat topics from the last 12 lines.
- Prefer variety of team-name alias.
"""


def build_arbiter_prompt(a_json, b_json, previous_calls, sub_hist, pitch_state,
                         alias_usage, latest_time_s):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-12:]) or "  - none"
    return f"""{ARBITER_PROMPT_HEAD}

VIDEO CLOCK: {latest_time_s:.1f}s

CANDIDATE A (gpt-5.4-mini):
  specific:   {a_json.get('specific') if a_json else None!r}
  safe:       {a_json.get('safe') if a_json else None!r}
  confidence: {a_json.get('confidence') if a_json else 'no_call'!r}

CANDIDATE B (Gemini 2.5 Flash):
  specific:   {b_json.get('specific') if b_json else None!r}
  safe:       {b_json.get('safe') if b_json else None!r}
  confidence: {b_json.get('confidence') if b_json else 'no_call'!r}

SUBS ALREADY ANNOUNCED (any candidate re-announcing → prefer NO_CALL):
{sub_hist}

PITCH STATE:
{pitch_state or "(starting XIs unchanged)"}

RECENT ACCEPTED LINES:
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

Produce arbiter JSON:"""


def arbiter_call(a_json, b_json, previous, sub_hist, pitch_state,
                 alias_usage, latest_time_s, newest_frame):
    """Returns (verdict_dict, latency_ms, raw_text)."""
    prompt = build_arbiter_prompt(a_json, b_json, previous, sub_hist,
                                  pitch_state, alias_usage, latest_time_s)
    content = [{"type": "input_text", "text": prompt}]
    if newest_frame:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(newest_frame)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=ARBITER_MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=400,
            temperature=0.3,
        )
        raw = (resp.output_text or '').strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            obj = json.loads(m.group(0))
            return obj, int((time.monotonic()-t0)*1000), raw
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"
    return None, int((time.monotonic()-t0)*1000), None


def call_both_visions(burst_paths, prompt):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_a = ex.submit(openai_vision_call, burst_paths, prompt)
        f_b = ex.submit(gemini_vision_call, burst_paths, prompt)
        a_json, a_ms, a_raw = f_a.result()
        b_json, b_ms, b_raw = f_b.result()
    return a_json, a_ms, a_raw, b_json, b_ms, b_raw


def resolve_choice(a_json, b_json, choice):
    """Given the arbiter's choice string, return the actual text.
    Also enforces: 'en' the arbiter returned must MATCH the picked option
    verbatim (safety guard against rewrites)."""
    slot_map = {
        'A_specific': (a_json or {}).get('specific'),
        'A_safe':     (a_json or {}).get('safe'),
        'B_specific': (b_json or {}).get('specific'),
        'B_safe':     (b_json or {}).get('safe'),
        'NO_CALL':    None,
    }
    return slot_map.get(choice)


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"Frames: {len(frame_paths)} | rich context: {len(rich_ctx)} chars")
    print(f"Vision A: {OAI_MODEL} | Vision B: {GEMINI_MODEL} | Arbiter: {ARBITER_MODEL} (with frame)")

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S,
                       ([prev_last] + current) if prev_last else current,
                       current[-1]))  # newest frame for the arbiter
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []; booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0
    choice_counts = {}
    t_start = time.time()
    last_print = time.time()

    for burst_idx, (i, latest_time_s, burst, newest) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-12:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs)
        pitch_state = format_pitch_state(ctx['roster'], subs)
        vision_prompt = build_vision_prompt(rich_ctx, latest_time_s, prev_texts,
                                            alias_usage, sub_hist, pitch_state)

        a_json, a_ms, a_raw, b_json, b_ms, b_raw = call_both_visions(burst, vision_prompt)
        vision_ms = max(a_ms, b_ms)

        # If BOTH models failed / returned no_call, skip the arbiter (saves cost)
        a_speaks = bool(a_json and a_json.get('specific'))
        b_speaks = bool(b_json and b_json.get('specific'))
        arbiter_json = None
        arb_ms = 0
        chosen = None
        choice = 'NO_CALL'
        fr_text = ''
        quality = 0
        reason = ''
        if a_speaks or b_speaks:
            arbiter_json, arb_ms, arb_raw = arbiter_call(
                a_json, b_json, prev_texts, sub_hist, pitch_state,
                alias_usage, latest_time_s, newest,
            )
            if arbiter_json:
                choice = arbiter_json.get('choice', 'NO_CALL')
                # Resolve verbatim (do NOT trust the 'en' rewrite)
                chosen = resolve_choice(a_json, b_json, choice)
                fr_text = arbiter_json.get('fr', '') or ''
                quality = arbiter_json.get('quality', 0)
                reason = arbiter_json.get('reason', '')

        choice_counts[choice] = choice_counts.get(choice, 0) + 1
        total_vision_ms = vision_ms + arb_ms

        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': total_vision_ms,
            'vision_parallel_ms': vision_ms,
            'arbiter_ms': arb_ms,
            'oai_ms': a_ms, 'gemini_ms': b_ms,
            'oai_json': a_json, 'gemini_json': b_json,
            'arbiter_json': arbiter_json,
            'choice': choice, 'quality': quality, 'reason': reason,
            'text': chosen, 'fr': fr_text,
            'accepted': False, 'reason_reject': None,
        }

        if chosen is None or is_no_call(chosen):
            no_call += 1; attempt['reason_reject'] = 'no_call'
        elif is_repetitive_trigram(chosen, [a['text'] for a in accepted], last_n=5):
            repetitive += 1; attempt['reason_reject'] = 'trigram_dup'
        else:
            sub = detect_sub(chosen, roster_by_short)
            if sub:
                if any(s['off']==sub[0] and s['on']==sub[1] for s in subs):
                    repetitive += 1; attempt['reason_reject'] = 'dup_sub'
                    all_attempts.append(attempt); continue
                on_pitch = {p['short_name'] for p in ctx['roster'] if p['role']=='starter'}
                for s in subs:
                    on_pitch.discard(s['off']); on_pitch.add(s['on'])
                if sub[0] not in on_pitch:
                    attempt['reason_reject'] = f'sub_off_not_on_pitch({sub[0]})'
                    all_attempts.append(attempt); continue
                if sub[1] in on_pitch:
                    attempt['reason_reject'] = f'sub_on_already_on_pitch({sub[1]})'
                    all_attempts.append(attempt); continue
                subs.append({'off': sub[0], 'on': sub[1], 'at_s': round(latest_time_s, 1)})

            est_tag = cheap_tag_guess(chosen)
            gate = gate_for_tag(est_tag)
            words = len(chosen.split())
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
            print(f"  b{burst_idx}/{len(bursts)} t={latest_time_s:.1f}s acc={len(accepted)} skip={skipped} nc={no_call} rep={repetitive} subs={len(subs)} el={time.time()-t_start:.0f}s ch={choice:<10} q={quality} last={(chosen or '')[:55]!r}")
            last_print = time.time()

    print(f"\nSummary: attempts={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors}")
    print(f"Choice counts: {choice_counts}")
    if accepted:
        lats = sorted(a['vision_latency_ms'] for a in accepted)
        vpar = sorted(a['vision_parallel_ms'] for a in accepted)
        arb = sorted(a['arbiter_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"total_ms  p50={pct(lats,0.5)}  p90={pct(lats,0.9)}")
        print(f"vision_par p50={pct(vpar,0.5)} p90={pct(vpar,0.9)}")
        print(f"arbiter    p50={pct(arb,0.5)} p90={pct(arb,0.9)}")
    print(f"Wall: {time.time()-t_start:.0f}s")
    print(f"Subs: {subs}")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# v8 hybrid — Vision {OAI_MODEL} + {GEMINI_MODEL} + Arbiter/Judge {ARBITER_MODEL}(with frame)\n")
        f.write(f"# {len(accepted)} accepted; choices {choice_counts}\n")
        f.write(f"# subs: {subs}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] q={a['quality']} {a['choice']:<12} EN: {a['text']}\n")
            f.write(f"                                    FR: {a['fr']}\n")
            if a.get('reason'):
                f.write(f"                                    why: {a['reason']}\n")
            f.write("\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

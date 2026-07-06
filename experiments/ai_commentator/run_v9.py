#!/usr/bin/env python3
"""AI commentator v9 — hallucination-first stack.

Adds to v8a:
  1. Playerist vision prompt on BOTH models — force player-name + generic verb
  2. Agreement-required for event verbs — if the picked line mentions any
     event verb (save/shot/tackle/sub/card/goal/etc), require the OTHER model
     to have mentioned the same verb; else downgrade to safe.
  3. Frame verifier — after arbiter picks, a cheap gpt-5.4-mini call sees the
     newest frame + the chosen line, answers verified|downgrade|no_call.
     Downgrade replaces the line with its "safe" counterpart from the picked
     model. no_call drops the line entirely.

Target: hallucination rate ≤ 8 %. Latency budget ~7 s p90.
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
OUT_JSONL = BASE / 'commentary_v9.jsonl'
OUT_KEPT = BASE / 'commentary_v9_kept.txt'
OUT_SCHED = BASE / 'commentary_v9_scheduled.jsonl'

OAI_MODEL = 'gpt-5.4-mini'
GEMINI_MODEL = 'gemini-2.5-flash'
ARBITER_MODEL = 'gpt-5.4-mini'
VERIFIER_MODEL = 'gpt-5.4-mini'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3

GEMINI_KEY = os.environ['GEMINI_API_KEY']
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
)
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


# Event verbs that trigger the "require agreement" check
EVENT_VERBS = {
    'save', 'saves', 'saved', 'denies', 'denied', 'claws', 'clawed',
    'punches', 'punched', 'catches', 'caught', 'claims', 'claimed',
    'tackle', 'tackles', 'tackled', 'intercept', 'intercepts', 'intercepted',
    'block', 'blocks', 'blocked', 'shot', 'shoots', 'fires', 'strikes',
    'struck', 'blasts', 'buries', 'buried', 'volleys', 'volleyed',
    'heads', 'headed', 'flicks', 'flicked', 'nutmegs', 'crosses',
    'crossed', 'delivers', 'delivered', 'whips', 'whipped',
    'foul', 'fouls', 'fouled', 'booked', 'booking', 'yellow', 'red',
    'goal', 'scored', 'scores', 'sub', 'subs', 'substitute', 'substitutes',
    'substituted', 'replaces', 'replaced', 'off for', 'on for',
    'nicks', 'nicked', 'wins', 'won', 'lost', 'clears', 'cleared',
    'applauds', 'celebrates', 'injured', 'injury', 'down',
    'sprawling', 'smother', 'stopped', 'halted', 'sent off',
    'scoring', 'award', 'awarded',
}


def contains_event_verb(text):
    if not text: return set()
    words = set(re.sub(r'[^\w\s]', ' ', text.lower()).split())
    return words & EVENT_VERBS


VISION_PLAYERIST_PROMPT = """You are a live English football commentator on a Bundesliga broadcast.

You see a burst of frames (oldest first, last one is NEWEST). Comment on
the NEWEST frame. First frame is carry-over — use it for continuity.

STRUCTURAL RULE — playerist mode:
- Almost EVERY spoken line should be [player-name] + [generic verb / role]
  + [optional location].
- Example: "Amiri drops back into midfield." "Klaus rolls it out from the box."
- ALMOST NEVER say "Mainz" / "Union" / any team alias in the specific.
  Naming a player already tells the viewer their team.
- Use a team name ONLY when: (a) announcing a substitution [sub-board visible];
  (b) describing a tactical shape change involving the whole team; (c) a
  restart where the player is genuinely unidentifiable.
- Do NOT claim specific events (save, tackle, shot, foul, sub) UNLESS the
  event is UNAMBIGUOUSLY mid-execution in the newest frame.

Produce JSON:

  "specific" — 5-12 words. Player + generic verb + location. Optionally weave
               a pre-game insight (from context) if it fits the moment.

  "safe"     — 5-10 words. If you can't confidently name a player, describe
               the ball location or team's tactical phase without event claims.

  "confidence" — "high" | "medium" | "low"

If nothing is worth commenting on:
  {"specific": null, "safe": null, "confidence": "no_call"}

HARD RULES:
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.
- DO NOT state the scoreline.
- Sub-board (4th official electronic panel): RED top = off, GREEN bottom = on.
  DO NOT re-announce a sub in the "SUBS ALREADY ANNOUNCED" list.
- GENERIC OVER INCORRECT.
"""


ARBITER_STRICT_PROMPT = """You are the ARBITER + JUDGE for a live football
commentary AI. You get:
  - The newest video frame
  - Two candidate outputs (each: specific + safe + confidence)
  - Running state

Job:
  1. Pick VERBATIM from: A_specific | A_safe | B_specific | B_safe | NO_CALL
  2. Reject specifics that claim events NOT visibly happening in the frame.
  3. Score 1-5.
  4. Translate to French.
  5. One-sentence reason.

Return JSON:
{"choice":"...","en":"<verbatim>","fr":"<translation>","quality":0-5,"reason":"..."}

STRICT FRAME-GROUNDING RUBRIC:
- Reject any claim about state NOT visible in this frame right now:
  * "injured" (only "down" is visible)
  * "on a booking" (context, not in frame)
  * kit-colour descriptions if the frame doesn't show it clearly
  * "just sent ball out" (only static ball is visible)
  * "substitute coming on" (unless the 4th-official board IS in the frame)
  * "applauds the crowd" (only "hand raised" is visible)
  * "celebrates a goal" (only visible = players together)
- Score ≤ 2 for lines with SUBSTITUTION / GOAL / CARD claims when the frame
  doesn't literally show those events.
- Prefer NO_CALL over ANY line scoring ≤ 2.
- When candidates disagree on the event, prefer NO_CALL unless a SAFE variant
  is neutrally correct (just player-on-ball + location, no event claim).
- Default: err on the side of NO_CALL. Silence beats a wrong claim.
"""


VERIFIER_PROMPT = """You are the FINAL FRAME VERIFIER for a live football
commentary line. You see:
  - The newest frame the commentators just spoke about
  - The proposed line
  - The proposed line's SAFE fallback (from the same model)

Answer with JSON:
{"verdict": "verified" | "downgrade" | "no_call", "reason": "<one short sentence>"}

verified   — the proposed line is faithful to what's visible in the frame
downgrade  — the specific claim is speculative; drop to the safe fallback
no_call    — even the safe is speculative; skip this burst entirely

BE STRICT:
- A specific event (save/shot/tackle/goal/card/sub/foul) must be visibly
  happening in the frame right now. If you can't see the event, downgrade.
- Narrative interpretive claims ("injured", "celebrates", "on a booking",
  "farewell", "just sent ball out") that aren't verifiable from a single
  frame → downgrade.
- Pure "player-on-ball-in-position" statements → verified.
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def build_vision_prompt(rich_ctx, latest_time_s, previous_calls, alias_usage,
                        sub_hist, pitch_state):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-12:]) or "  - none"
    return f"""{VISION_PLAYERIST_PROMPT}

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

Produce JSON:"""


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


def call_both_visions(burst_paths, prompt):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_a = ex.submit(openai_vision_call, burst_paths, prompt)
        f_b = ex.submit(gemini_vision_call, burst_paths, prompt)
        a_json, a_ms, a_raw = f_a.result()
        b_json, b_ms, b_raw = f_b.result()
    return a_json, a_ms, a_raw, b_json, b_ms, b_raw


def build_arbiter_prompt(a_json, b_json, previous_calls, sub_hist, pitch_state,
                         alias_usage, latest_time_s):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-12:]) or "  - none"
    return f"""{ARBITER_STRICT_PROMPT}

VIDEO CLOCK: {latest_time_s:.1f}s

CANDIDATE A (gpt-5.4-mini):
  specific:   {a_json.get('specific') if a_json else None!r}
  safe:       {a_json.get('safe') if a_json else None!r}
  confidence: {a_json.get('confidence') if a_json else 'no_call'!r}

CANDIDATE B (Gemini 2.5 Flash):
  specific:   {b_json.get('specific') if b_json else None!r}
  safe:       {b_json.get('safe') if b_json else None!r}
  confidence: {b_json.get('confidence') if b_json else 'no_call'!r}

SUBS ALREADY ANNOUNCED:
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
            max_output_tokens=500,
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


def resolve_choice(a_json, b_json, choice):
    slot_map = {
        'A_specific': (a_json or {}).get('specific'),
        'A_safe':     (a_json or {}).get('safe'),
        'B_specific': (b_json or {}).get('specific'),
        'B_safe':     (b_json or {}).get('safe'),
        'NO_CALL':    None,
    }
    return slot_map.get(choice)


def resolve_safe_for_choice(a_json, b_json, choice):
    """Return the SAFE counterpart of the chosen slot."""
    src = a_json if choice.startswith('A_') else (b_json if choice.startswith('B_') else None)
    return (src or {}).get('safe') if src else None


def verifier_call(chosen_line, safe_line, newest_frame):
    prompt = f"""{VERIFIER_PROMPT}

PROPOSED LINE: {chosen_line!r}
SAFE FALLBACK: {safe_line!r}

Answer JSON:"""
    content = [{"type": "input_text", "text": prompt}]
    if newest_frame:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(newest_frame)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=VERIFIER_MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=200,
            temperature=0.1,
        )
        raw = (resp.output_text or '').strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group(0)), int((time.monotonic()-t0)*1000)
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000)
    return None, int((time.monotonic()-t0)*1000)


def enforce_agreement(chosen_text, a_json, b_json, choice):
    """If chosen line contains an event verb, require the OTHER model to have
    mentioned the same verb. Otherwise return (None, 'agreement_fail')."""
    if not chosen_text:
        return chosen_text, None
    verbs_in_chosen = contains_event_verb(chosen_text)
    if not verbs_in_chosen:
        return chosen_text, None
    # Check the OTHER model's specific + safe
    if choice.startswith('A_'):
        other = b_json or {}
    else:
        other = a_json or {}
    other_text = (other.get('specific') or '') + ' ' + (other.get('safe') or '')
    other_verbs = contains_event_verb(other_text)
    if verbs_in_chosen & other_verbs:
        return chosen_text, 'agreement_ok'
    return None, 'agreement_fail'


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"v9: Vision {OAI_MODEL}+{GEMINI_MODEL}, Arbiter {ARBITER_MODEL}+frame, Verifier {VERIFIER_MODEL}+frame")
    print(f"Frames: {len(frame_paths)} | rich context: {len(rich_ctx)} chars")

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S,
                       ([prev_last] + current) if prev_last else current,
                       current[-1]))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []; booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0
    downgrade_count = 0; agreement_fails = 0; verifier_no_call = 0
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

        a_any = bool(a_json and (a_json.get('specific') or a_json.get('safe')))
        b_any = bool(b_json and (b_json.get('specific') or b_json.get('safe')))
        arbiter_json = None; arb_ms = 0; verifier_ms = 0
        chosen = None; choice = 'NO_CALL'
        fr_text = ''; quality = 0; reason = ''
        v_verdict = None
        agreement_check = None

        if a_any or b_any:
            arbiter_json, arb_ms, arb_raw = arbiter_call(
                a_json, b_json, prev_texts, sub_hist, pitch_state,
                alias_usage, latest_time_s, newest)
            if arbiter_json:
                choice = arbiter_json.get('choice', 'NO_CALL')
                chosen = resolve_choice(a_json, b_json, choice)
                fr_text = arbiter_json.get('fr', '') or ''
                quality = arbiter_json.get('quality', 0)
                reason = arbiter_json.get('reason', '')

                # STAGE 1: agreement check on event verbs
                if chosen:
                    chosen_after_agree, agreement_check = enforce_agreement(
                        chosen, a_json, b_json, choice)
                    if agreement_check == 'agreement_fail':
                        # Downgrade to safe of the same choice
                        safe_alt = resolve_safe_for_choice(a_json, b_json, choice)
                        if safe_alt and not (contains_event_verb(safe_alt)):
                            chosen = safe_alt
                            choice = choice.split('_')[0] + '_safe'
                            downgrade_count += 1
                            agreement_fails += 1
                        else:
                            chosen = None
                            choice = 'NO_CALL'
                            agreement_fails += 1

                # STAGE 2: frame verifier
                if chosen:
                    safe_alt = resolve_safe_for_choice(a_json, b_json, choice)
                    v_verdict, verifier_ms = verifier_call(chosen, safe_alt, newest)
                    if v_verdict:
                        verdict = v_verdict.get('verdict', 'verified')
                        if verdict == 'downgrade' and safe_alt:
                            chosen = safe_alt
                            choice = choice.split('_')[0] + '_safe'
                            downgrade_count += 1
                        elif verdict == 'no_call':
                            chosen = None
                            choice = 'NO_CALL'
                            verifier_no_call += 1

        choice_counts[choice] = choice_counts.get(choice, 0) + 1
        total_ms = vision_ms + arb_ms + verifier_ms

        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': total_ms,
            'vision_parallel_ms': vision_ms, 'arbiter_ms': arb_ms, 'verifier_ms': verifier_ms,
            'oai_ms': a_ms, 'gemini_ms': b_ms,
            'oai_json': a_json, 'gemini_json': b_json,
            'arbiter_json': arbiter_json, 'verifier_verdict': v_verdict,
            'agreement_check': agreement_check,
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
            print(f"  b{burst_idx}/{len(bursts)} t={latest_time_s:.1f}s acc={len(accepted)} skip={skipped} nc={no_call} rep={repetitive} downgrade={downgrade_count} el={time.time()-t_start:.0f}s ch={choice:<10} last={(chosen or '')[:55]!r}")
            last_print = time.time()

    print(f"\nv9 Summary: attempts={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors}")
    print(f"Agreement fails: {agreement_fails}  Verifier no_call: {verifier_no_call}  Downgrades total: {downgrade_count}")
    print(f"Choice counts: {choice_counts}")
    if accepted:
        lats = sorted(a['vision_latency_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"total_ms p50={pct(lats,0.5)} p90={pct(lats,0.9)}")
    print(f"Wall: {time.time()-t_start:.0f}s")
    print(f"Subs: {subs}")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# v9: playerist vision + strict arbiter + agreement-required + frame verifier\n")
        f.write(f"# {len(accepted)} accepted; choices {choice_counts}\n")
        f.write(f"# agreement_fails={agreement_fails} verifier_no_call={verifier_no_call} downgrades={downgrade_count}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] q={a['quality']} {a['choice']:<12} EN: {a['text']}\n")
            f.write(f"                                    FR: {a['fr']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

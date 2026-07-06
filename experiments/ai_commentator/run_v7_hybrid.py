#!/usr/bin/env python3
"""AI commentator v7 — dual-model hybrid, specific + safe fallback per call.

Each burst: gpt-5.4-mini AND gemini-2.5-flash in parallel. Both output structured
JSON: {"specific": ..., "safe": ..., "confidence": ...}. Merge rules:

  Both no_call                    → NO_CALL (silence acceptable when nothing's happening)
  Both speak, same subject        → use the SPECIFIC (shorter one preferred)
  Both speak, different subjects  → use SAFE fallback (silence beats wrong)
  Only one speaks                 → use that one's SAFE (specific may be a hallu)

Result:
  - Hallucinations only survive if BOTH models independently make the same one
    (rare — different training corpora)
  - Coverage stays high because SAFE fallback fills the disagreement gap
  - Latency ≈ max(both models) ≈ 2.5 s → fits under 3 s pipeline budget

Outputs commentary_v7.jsonl + commentary_v7_scheduled.jsonl.
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
    build_match_context, build_match_context_text, is_no_call,
)

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v7.jsonl'
OUT_KEPT = BASE / 'commentary_v7_kept.txt'
OUT_SCHED = BASE / 'commentary_v7_scheduled.jsonl'

OAI_MODEL = 'gpt-5.4-mini'
GEMINI_MODEL = 'gemini-2.5-flash'
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3

GEMINI_KEY = os.environ['GEMINI_API_KEY']
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
)

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


HYBRID_PROMPT = """You are a live English football commentator on a Bundesliga broadcast.

You see a burst of frames (oldest first, last one is NEWEST). Comment on the
NEWEST frame. The first frame is carry-over from the previous burst — use it
for continuity.

Produce TWO variants of what to say in your output JSON:

  "specific" — 5-12 words. Detailed play-by-play: name the player if you can
               see the shirt number, describe visible action. You MAY use
               event verbs (save, tackle, shot, cross, breakaway) BUT ONLY if
               the event is clearly mid-execution in the NEWEST frame.
               If unsure, do not include event verbs in specific.

  "safe"     — 3-8 words. GENERIC fallback: who is on the ball and where.
               NO event verbs. NO specific outcomes. This must be TRUE from
               the frame at surface level ("Burke on the ball", "Klaus alert
               in goal", "Amiri over the dead ball"). This is what will be
               spoken if another AI's "specific" disagrees with yours.

  "confidence" — "high" | "medium" | "low"
     high   = shirt number legible, action clearly identifiable
     medium = position + kit colour → player guess is likely right
     low    = uncertain who or what

If nothing significant is happening (routine possession, replay, crowd shot,
static image), output:
  {"specific": null, "safe": null, "confidence": "no_call"}

Otherwise output:
  {"specific": "...", "safe": "...", "confidence": "high|medium|low"}

Return ONLY valid JSON, nothing else.

RULES:
- Use the alias bank sparingly. Naming a player already implies their team.
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.
- DO NOT state the scoreline (it's on screen).
- Sub-board (fourth-official electronic panel): RED top = off, GREEN bottom = on;
  DO NOT re-announce a sub already in the list below.
- GENERIC OVER INCORRECT: when the specific claim would be a guess, downgrade
  to a description that is verifiable from the frame.
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def build_full_prompt(ctx_text, latest_time_s, previous_calls, alias_usage,
                     sub_hist, pitch_state):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none"
    return f"""{HYBRID_PROMPT}

VIDEO CONTEXT
Current video clock: {latest_time_s:.1f}s.

SUB HISTORY (do not re-announce):
{sub_hist}

PITCH STATE:
{pitch_state or "(no subs yet)"}

MATCH CONTEXT
{ctx_text}

RECENT CALLS (do not repeat):
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

Produce JSON:"""


def parse_json_response(text):
    """Extract JSON from a model output, tolerant of surrounding text."""
    if not text:
        return None
    m = re.search(r'\{.*?\}', text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return {
            'specific': obj.get('specific') or None,
            'safe': obj.get('safe') or None,
            'confidence': obj.get('confidence') or 'medium',
        }
    except json.JSONDecodeError:
        return None


def openai_call(burst_paths, prompt):
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=OAI_MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=200,
            temperature=0.5,
        )
        out = (resp.output_text or '').strip()
        return parse_json_response(out), int((time.monotonic()-t0)*1000), out
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"


def gemini_call(burst_paths, prompt):
    parts = [{"text": prompt}]
    for p in burst_paths:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": encode_jpeg(p)}})
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 200,
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


def call_both_parallel(burst_paths, prompt):
    """Return (oai_result, gemini_result, latency_ms_max)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_a = ex.submit(openai_call, burst_paths, prompt)
        f_b = ex.submit(gemini_call, burst_paths, prompt)
        a_json, a_ms, a_raw = f_a.result()
        b_json, b_ms, b_raw = f_b.result()
    return a_json, a_ms, a_raw, b_json, b_ms, b_raw


def extract_subject_tokens(text, roster_names, aliases):
    """Return a set of "subject tokens" found in text — player names, team
    short-names, aliases. These are matched between models to decide agreement."""
    if not text: return set()
    tokens = set()
    for name in roster_names:
        if re.search(rf'\b{re.escape(name)}\b', text, re.I):
            tokens.add(f"player:{name.lower()}")
    for team_key, data in aliases.items():
        short = data.get('short', team_key)
        # Only add team tokens if the alias literally appears (not just when a
        # player is named — we want *explicit* team refs for agreement).
        for cat, items in data['aliases'].items():
            for alias in items:
                if re.search(rf'\b{re.escape(alias)}\b', text, re.I):
                    tokens.add(f"team:{short.lower()}")
                    break
    return tokens


def has_event_verb(text):
    """Detect if the text claims a specific event (save/tackle/shot/etc).
    Used to decide when disagreement should downgrade to safe."""
    if not text: return False
    EVENT_VERBS = {
        'saves', 'saved', 'save', 'denies', 'denied', 'claws', 'clawed',
        'punches', 'punched', 'claims', 'claimed', 'catches', 'caught',
        'tackles', 'tackled', 'intercepts', 'intercepted', 'blocks', 'blocked',
        'shoots', 'shot', 'fires', 'strikes', 'struck', 'blasts', 'buries', 'buried',
        'heads', 'headed', 'flicks', 'flicked', 'volleys', 'nutmegs',
        'whips', 'whipped', 'delivers', 'delivered', 'crosses', 'crossed',
        'nicks', 'nicked', 'nipped', 'wins', 'won', 'scored', 'scores',
        'yellow', 'red card', 'booking', 'booked',
    }
    t = re.sub(r'[^\w\s]', ' ', text.lower())
    words = set(t.split())
    return bool(words & EVENT_VERBS)


def merge_outputs(a, b, roster_names, aliases):
    """Return (chosen_text, decision_source) where decision_source ∈
    {'both_no_call', 'both_agree_specific', 'agree_but_no_event',
     'disagree_use_safe', 'one_speaks_use_safe', 'both_null'}."""
    a_spec = a.get('specific') if a else None
    a_safe = a.get('safe') if a else None
    b_spec = b.get('specific') if b else None
    b_safe = b.get('safe') if b else None

    a_speaks = bool(a_spec)
    b_speaks = bool(b_spec)

    if not a and not b:
        return None, 'both_null'
    if not a_speaks and not b_speaks:
        return None, 'both_no_call'

    if a_speaks and b_speaks:
        subj_a = extract_subject_tokens(a_spec, roster_names, aliases)
        subj_b = extract_subject_tokens(b_spec, roster_names, aliases)
        shared = subj_a & subj_b
        if shared:
            # Agreement on subject — safe to use a specific
            # Prefer the higher-confidence one; tie-break by shorter text.
            conf_rank = {'high': 3, 'medium': 2, 'low': 1, 'no_call': 0}
            a_c = conf_rank.get((a or {}).get('confidence', 'medium'), 2)
            b_c = conf_rank.get((b or {}).get('confidence', 'medium'), 2)
            if a_c > b_c: return a_spec, 'both_agree_specific'
            if b_c > a_c: return b_spec, 'both_agree_specific'
            # Same confidence: prefer shorter (usually more definite)
            return (a_spec if len(a_spec) <= len(b_spec) else b_spec), 'both_agree_specific'
        else:
            # Disagree on subject → downgrade to safe fallback
            # Prefer the safe that names ANY player (more informative)
            for candidate in (a_safe, b_safe):
                if candidate and extract_subject_tokens(candidate, roster_names, aliases):
                    return candidate, 'disagree_use_safe'
            return (a_safe or b_safe), 'disagree_use_safe'

    # One speaks, one no_call
    speaker = a if a_speaks else b
    # Trust the safe fallback more than the specific in disagreement mode
    return (speaker.get('safe') or speaker.get('specific')), 'one_speaks_use_safe'


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']
    roster_names = {p['short_name'] for p in ctx['roster']}
    # also include family + given names
    for p in ctx['roster']:
        for part in re.split(r'[\s,-]+', p['name']):
            if part[0:1].isupper() and len(part) > 2:
                roster_names.add(part.strip(',.'))
    print(f"Frames: {len(frame_paths)} | roster names: {len(roster_names)}")
    print(f"Models: OAI={OAI_MODEL}  Gemini={GEMINI_MODEL}")

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S,
                       ([prev_last] + current) if prev_last else current))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    subs = []
    booth_busy_until = 0.0
    no_call=0; repetitive=0; errors=0; skipped=0
    decision_counts = {}
    t_start = time.time()
    last_print = time.time()

    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + 0.05:
            skipped += 1; continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        sub_hist = format_sub_history(subs)
        pitch_state = format_pitch_state(ctx['roster'], subs)
        prompt = build_full_prompt(ctx_text, latest_time_s, prev_texts,
                                   alias_usage, sub_hist, pitch_state)

        a_json, a_ms, a_raw, b_json, b_ms, b_raw = call_both_parallel(burst, prompt)
        vision_ms = max(a_ms, b_ms)  # parallel wall-clock
        chosen, decision = merge_outputs(a_json, b_json, roster_names, aliases)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': vision_ms,
            'oai_ms': a_ms, 'gemini_ms': b_ms,
            'oai_json': a_json, 'gemini_json': b_json,
            'oai_raw': a_raw, 'gemini_raw': b_raw,
            'decision': decision, 'text': chosen,
            'accepted': False, 'reason': None,
        }

        if chosen is None or is_no_call(chosen):
            no_call += 1; attempt['reason'] = 'no_call'
        elif is_repetitive_trigram(chosen, [a['text'] for a in accepted], last_n=5):
            repetitive += 1; attempt['reason'] = 'trigram_dup'
        else:
            # Sub check
            sub = detect_sub(chosen, {p['short_name']: p for p in ctx['roster']})
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
            print(f"  b{burst_idx}/{len(bursts)} t={latest_time_s:.1f}s acc={len(accepted)} skip={skipped} no_call={no_call} subs={len(subs)} elapsed={time.time()-t_start:.0f}s decision={decision} last={(chosen or '')[:60]!r}")
            last_print = time.time()

    print(f"\nSummary: attempts={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive}")
    print(f"Decisions: {decision_counts}")
    if accepted:
        lats = sorted(a['vision_latency_ms'] for a in accepted)
        oai_lats = sorted(a['oai_ms'] for a in accepted)
        gem_lats = sorted(a['gemini_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision_max p50={pct(lats,0.5)}ms p90={pct(lats,0.9)}ms")
        print(f"oai p50={pct(oai_lats,0.5)}ms p90={pct(oai_lats,0.9)}ms")
        print(f"gemini p50={pct(gem_lats,0.5)}ms p90={pct(gem_lats,0.9)}ms")
    print(f"Wall: {time.time()-t_start:.0f}s")
    print(f"Subs: {subs}")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts: f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted: f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# v7 hybrid — {OAI_MODEL} + {GEMINI_MODEL}\n# {len(accepted)} accepted\n# Subs: {subs}\n# Decisions: {decision_counts}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['decision']:<22} {a['text']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

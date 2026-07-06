#!/usr/bin/env python3
"""AI commentator v5 — adds on top of v4:
  1. Sub-event state across calls (off/on pairs tracked, never re-announced)
  2. Currently-on-pitch tracker (model knows who left, who joined)
  3. Trigram dedup (catches "Amiri over it" / "Amiri over this dead ball")
  4. Frame carry-over (last frame of previous burst is first frame of this one)
  5. Dynamic booth-busy gate (longer during [calm]/[flatly] tagged lines)
  6. Stronger NO_CALL guidance ("be quiet during routine possession")

Keeps EVERYTHING from v4: alias rotation, scoreline rule, sub-board recognition,
set-piece attribution, pre-game player insights, manager context, style examples.

Outputs commentary_v5.jsonl + commentary_v5_scheduled.jsonl.
"""
from __future__ import annotations
import base64, json, os, re, sys, time, wave
import urllib.request, urllib.error
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# pull in v4 helpers (alias loading, roster parsing, context build)
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v4 import (
    _parse_roster_text, _load_sr_positions, _load_team_aliases,
    _format_roster_block, _format_alias_block,
    PLAYER_INSIGHTS, MANAGER_INSIGHTS, STYLE_EXAMPLES,
    summarise_alias_usage,
)

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v5.jsonl'
OUT_KEPT = BASE / 'commentary_v5_kept.txt'
OUT_SCHED = BASE / 'commentary_v5_scheduled.jsonl'

SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.4-mini'
MAX_OUTPUT_TOKENS = 50
TEMPERATURE = 0.55
SR_TTS = 16000
DURATION_S = 300.0
NATURAL_LAG_S = 0.3

# Dynamic booth-busy gates
GATE_CALM_S = 4.0       # after a [calm]/[flatly]/[deadpan] line, wait longer
GATE_NORMAL_S = 1.8     # after action lines
GATE_TAG_CALM = {'[calm]', '[flatly]', '[deadpan]', '[resigned tone]'}


def trigrams(text):
    """Return set of word trigrams from a text, lowercased, punctuation stripped."""
    words = re.sub(r'[^\w\s]', ' ', text.lower()).split()
    return {tuple(words[i:i+3]) for i in range(len(words)-2)} if len(words) >= 3 else set()


def is_repetitive_trigram(text, previous_texts, last_n=5):
    """Reject if shares any trigram with the last N accepted lines."""
    if not text: return False
    new = trigrams(text)
    if not new: return False
    for prev in previous_texts[-last_n:]:
        if new & trigrams(prev):
            return True
    return False


# --- substitution state -----------------------------------------------------

SUB_PATTERNS = [
    # "Becker off, Sieb on for the hosts"
    re.compile(r'(?P<off>[A-Z][\w-]+)\s+off[\s,]+(?P<on>[A-Z][\w-]+)\s+on', re.I),
    # "Sieb on, Tietz off"
    re.compile(r'(?P<on>[A-Z][\w-]+)\s+on[\s,]+(?P<off>[A-Z][\w-]+)\s+off', re.I),
    # "Sieb on for Becker"
    re.compile(r'(?P<on>[A-Z][\w-]+)\s+on\s+for\s+(?P<off>[A-Z][\w-]+)', re.I),
    # "Juranovic replaces Trimmel"
    re.compile(r'(?P<on>[A-Z][\w-]+)\s+replaces\s+(?P<off>[A-Z][\w-]+)', re.I),
    # "Weiper over the dead ball, Tietz off"
    re.compile(r'(?P<on>[A-Z][\w-]+)\s+over\s+.+?[,\s]+(?P<off>[A-Z][\w-]+)\s+off', re.I),
    # "off comes Becker, on comes Sieb"
    re.compile(r"off\s+(?:comes|goes)\s+(?P<off>[A-Z][\w-]+)[\s,.]+on\s+(?:comes|goes)\s+(?P<on>[A-Z][\w-]+)", re.I),
]


SOFT_NO_CALL_RE = re.compile(
    r"^\s*(no[_ ]?call|n/a|—|-+|no real (action|urgency|opening|incision|pressure)\b.*|nothing (?:new |yet ?)$)",
    re.I)


def detect_sub(text, roster_by_short):
    """Return (off, on) tuple if text mentions a sub matching roster, else None."""
    for pat in SUB_PATTERNS:
        m = pat.search(text)
        if not m: continue
        off = m.group('off').capitalize()
        on = m.group('on').capitalize()
        # require both to be in the roster
        if off in roster_by_short and on in roster_by_short:
            return (off, on)
    return None


def format_sub_history(subs):
    if not subs:
        return "  (none announced yet)"
    lines = []
    for s in subs:
        lines.append(f"  - {s['off']} off, {s['on']} on (announced at {s['at_s']:.0f}s)")
    return "\n".join(lines)


def format_pitch_state(roster, subs):
    """Return concise summary of who's on the bench having been subbed off and who came on."""
    if not subs:
        return ""
    off_set = {s['off'] for s in subs}
    on_set = {s['on'] for s in subs}
    return f"OFF the pitch (subbed off): {', '.join(sorted(off_set))}.  Now on the pitch (subbed on): {', '.join(sorted(on_set))}."


# --- prompt -----------------------------------------------------------------

def build_visual_prompt(ctx_text, latest_time_s, previous_calls, alias_usage_summary,
                       sub_history_text, pitch_state_text):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none yet"
    examples = "\n".join(f"  - \"{e}\"" for e in STYLE_EXAMPLES)
    return f"""You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers.

PROFILE: experienced English-language sportscaster. Short, sharp, urgent during
attacks; reflective during lulls; restrained when the picture is unclear.

VIDEO CONTEXT
Current video clock: {latest_time_s:.1f}s. You see a short burst of frames,
oldest first, newest last. The FIRST frame is also the last frame from the
PREVIOUS burst — use it to track continuity with what you just said. Comment
on whatever is happening in the NEWEST frame.

OUTPUT — KEEP IT SHORT, KEEP IT SPARSE
- 3-12 words, one sentence or a fragment.
- NO_CALL is the right answer about 40-50% of the time. A skilled commentator
  goes QUIET during routine possession — speech is reserved for moments of
  consequence (entries into the final third, set pieces, shots, saves, subs,
  cards, notable individual moments). Default to NO_CALL when in doubt.
- If you'd just be paraphrasing your previous call with different wording,
  return NO_CALL. Repetition kills the broadcast.
- Variety when you DO speak: action calls, named identifications, pass-type
  observations, tactical reads, time references, atmosphere, terse outcomes,
  pre-game-note asides.

NAMING — LEAN INTO IT
- NAME PLAYERS WHENEVER REASONABLE. Strong identification is the heart of
  good football commentary.
- Use the shirt number visible on the back when you can. Use kit colour +
  position + roster to pick the most likely name when not.
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either squad's roster.

SUBSTITUTIONS — TWO HARD RULES
1. SUBS ALREADY ANNOUNCED THIS HALF:
{sub_history_text}
   DO NOT re-announce any of these. If you see the same fourth-official
   board again it is the replay of a sub you already covered. Move on.
2. {pitch_state_text or "(no subs yet; full starting XIs are still on the pitch)"}
   When you see the fourth-official's electronic board (RED top number =
   off, GREEN bottom = on), state it as a substitution and name BOTH
   players via the roster — but ONLY if it's not in the list above.

TEAM-NAME VARIETY — STRICT
- Repeating "Mainz" or "Union" line after line sounds robotic. STOP.
- Use the alias bank: role tokens (the hosts/visitors), nicknames (the
  05ers / die Eisernen), kit colours, manager possessives, place names.
- Often you don't need to name a team at all — naming a player already
  implies their side.
- At most ONE explicit team reference per line. Often zero is best.
- Check the alias-usage block below for which names to AVOID this turn.

SCORELINE RULE
- DO NOT state the scoreline. The score graphic is on screen.
- Exceptions: (a) something just changed it (goal / disallowed goal in
  this burst); (b) inside the final five minutes for tension.

SET-PIECE TEAM ATTRIBUTION
- Throw / FK / corner / goal kick: only name a team when you can clearly
  see which side picks up / stands over the ball. Cross-check the shirt
  number against the rosters.
- Otherwise describe the set piece WITHOUT naming a team.

USING PRE-GAME NOTES
- The squad block lists notes for some players. If a named player has a
  note that fits, briefly weave it in. Once per session per player.

NO INVENTION
- No goals, cards, scores, substitutions, or events not visibly happening.
- The known final result is private metadata — do not reference it.

STYLE EXAMPLES (illustrative; do NOT transcribe verbatim):
{examples}

CONTEXT
{ctx_text}

RECENT CALLS — do not repeat the same observation:
{previous}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage_summary}

Produce your next call (or NO_CALL):
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, burst_paths, latest_time_s, previous_calls, ctx_text,
                alias_usage_summary, sub_history_text, pitch_state_text):
    content = [{"type": "input_text",
                "text": build_visual_prompt(ctx_text, latest_time_s, previous_calls,
                                            alias_usage_summary, sub_history_text, pitch_state_text)}]
    for p in burst_paths:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
        )
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"{type(e).__name__}: {str(e)[:200]}"


def build_match_context():
    """Local copy because v4's includes its own paths — keep parallel."""
    roster_text = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/roster.json'))['roster_text']
    roster = _parse_roster_text(roster_text)
    positions = _load_sr_positions()
    for p in roster:
        p['position'] = positions.get(p['name'], '')
        p['insight'] = PLAYER_INSIGHTS.get(p['short_name'], '')
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    formations = {c['name']: c.get('formation', {}).get('type', '?')
                  for c in sr['lineups']['lineups']['competitors']}
    aliases = _load_team_aliases() or {}
    return {
        'title': 'FSV Mainz vs Union Berlin, Bundesliga matchday 33',
        'venue': 'Mewa Arena, Mainz',
        'home_team': 'FSV Mainz', 'home_abbr': 'M05',
        'away_team': 'Union Berlin', 'away_abbr': 'UNI',
        'home_color': 'predominantly white shirts with red trim',
        'away_color': 'predominantly red shirts',
        'home_formation': formations.get('FSV Mainz', '?'),
        'away_formation': formations.get('Union Berlin', '?'),
        'roster': roster,
        'aliases': aliases,
        'storyline': "Both sides safe in the table but jockeying for league position.",
    }


def build_match_context_text(ctx):
    parts = [
        f"Match: {ctx['title']}, at {ctx['venue']}.",
        f"Storyline: {ctx['storyline']}",
        f"{ctx['home_team']} ({ctx['home_abbr']}): formation {ctx['home_formation']}, {ctx['home_color']}.",
        f"{ctx['away_team']} ({ctx['away_abbr']}): formation {ctx['away_formation']}, {ctx['away_color']}.",
        f"  Union manager: {MANAGER_INSIGHTS.get('Eta', '')}",
        f"  Union ex-manager context: {MANAGER_INSIGHTS.get('Fischer', '')}",
        _format_alias_block(ctx['aliases']),
        _format_roster_block(ctx['roster'], ctx['home_team'], ctx['home_abbr']),
        _format_roster_block(ctx['roster'], ctx['away_team'], ctx['away_abbr']),
    ]
    return "\n".join(parts)


NO_CALL_RE = re.compile(r"^\s*(no[_ ]?call|n/a|—|-+)\s*$", re.I)
def is_no_call(text):
    if not text: return False
    if NO_CALL_RE.match(text): return True
    if SOFT_NO_CALL_RE.match(text): return True
    return False


def gate_for_tag(tag):
    return GATE_CALM_S if (tag in GATE_TAG_CALM) else GATE_NORMAL_S


def cheap_tag_guess(text):
    """Cheap heuristic to estimate the emotion tag of a line WITHOUT calling
    the tagging LLM. Used only to size the booth-busy window."""
    t = text.lower()
    if any(k in t for k in ('!', 'denies', 'breakaway', 'driving', 'breaks', 'whip', 'scramble', 'load the box', 'final third', 'shot')):
        return '[excited]'
    if any(k in t for k in ('down', 'injury', 'on his head', 'medical', 'hands on hips')):
        return '[sorrowful]'
    if any(k in t for k in ('off,', 'replaces', 'reshuffle', 'on for', ' off ')):
        return '[resigned tone]'
    return '[calm]'


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Frames: {len(frame_paths)}")
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"Context size: {len(ctx_text)} chars; aliases loaded: {bool(aliases)}; roster: {len(roster_by_short)} players")
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        # carry-over: prepend last frame of previous burst as first frame of this burst
        prev_last = frame_paths[i - CONTEXT_FRAMES] if i >= CONTEXT_FRAMES else None
        current = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        burst_frames = ([prev_last] + current) if prev_last else current
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, burst_frames))
    print(f"Bursts: {len(bursts)} (each call sees {CONTEXT_FRAMES + 1} frames: 1 carry-over + 4 current)")

    accepted = []
    all_attempts = []
    subs = []  # [{'off':..., 'on':..., 'at_s':...}]
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

        text, vision_ms, err = call_vision(
            client, burst, latest_time_s, prev_texts, ctx_text,
            alias_usage, sub_history_text, pitch_state_text)
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
            # detect sub mention
            sub = detect_sub(text, roster_by_short)
            if sub:
                # reject if same sub already announced
                already = any(s['off'] == sub[0] and s['on'] == sub[1] for s in subs)
                if already:
                    repetitive += 1; attempt['reason']='dup_sub_announce'
                    all_attempts.append(attempt)
                    continue
                # also reject if either player is in a contradictory state
                # (claiming player X is coming on when they were already subbed on; or coming off when they're not on the pitch)
                on_pitch = {p['short_name'] for p in ctx['roster'] if p['role'] == 'starter'}
                for s in subs:
                    on_pitch.discard(s['off'])
                    on_pitch.add(s['on'])
                if sub[0] not in on_pitch:
                    attempt['reason'] = f'sub_off_not_on_pitch({sub[0]})'
                    all_attempts.append(attempt)
                    continue
                if sub[1] in on_pitch:
                    attempt['reason'] = f'sub_on_already_on_pitch({sub[1]})'
                    all_attempts.append(attempt)
                    continue
                subs.append({'off': sub[0], 'on': sub[1], 'at_s': round(latest_time_s, 1)})

            est_tag = cheap_tag_guess(text)
            gate = gate_for_tag(est_tag)
            words = len(text.split())
            est_duration_s = max(1.2, words / 3.0)
            scheduled_start_s = latest_time_s + NATURAL_LAG_S
            scheduled_end_s = scheduled_start_s + est_duration_s + (gate - GATE_NORMAL_S)
            attempt.update({
                'accepted': True,
                'est_tag': est_tag,
                'gate_s': gate,
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

    print(f"\nSummary: vision={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep/dup={repetitive} err={errors} subs={len(subs)}")
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
        f.write(f"# AI commentary v5 — sub memory + trigram dedup + frame carry-over + dynamic gate\n")
        f.write(f"# {len(accepted)} accepted of {len(all_attempts)} calls\n")
        f.write(f"# Subs tracked: {subs}\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

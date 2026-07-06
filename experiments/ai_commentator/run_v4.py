#!/usr/bin/env python3
"""AI commentator v4 — adds team alias rotation, scoreline rule, sub-board
recognition, set-piece team-attribution guardrail, and filler reduction.

All rules are GENERIC and built from info known PRE-GAME (rosters, team
aliases, club nicknames). No live-game knowledge.

Outputs commentary_v4.jsonl + commentary_v4_scheduled.jsonl + audio track WAV.
"""
from __future__ import annotations
import base64, json, os, re, sys, time, subprocess, wave
import urllib.request, urllib.error
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v4.jsonl'
OUT_KEPT = BASE / 'commentary_v4_kept.txt'
OUT_SCHED = BASE / 'commentary_v4_scheduled.jsonl'
SOURCE_MP4 = Path('/tmp/v2v_compare/slice_5min.mp4')

SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.4-mini'
MAX_OUTPUT_TOKENS = 50
TEMPERATURE = 0.55
SR_TTS = 16000
DURATION_S = 300.0
DEDUP_JACCARD = 0.4
NATURAL_LAG_S = 0.3  # AI line plays this long after the visible moment


PLAYER_INSIGHTS = {
    'Burke': "In Scotland's World Cup squad picture; Eta gives him longer runs than Baumgart did.",
    'Sieb': "On a two-year loan from Bayern, leaving at season end; received a formal farewell pre-match.",
    'Doekhi': "Dutch centre-back, born in Rotterdam; English clubs watching him.",
    'Weiper': "Tall striker; Germany U21; hasn't had things go his way this campaign.",
    'Kohn': "The 'Derrick' name is a long-running joke with the German detective TV series.",
    'Zentner': "Strong at coming for crosses; one-on-ones not his main strength.",
    'Veratschnig': "Very good recovery pace at the back.",
    'Juranovic': "Coming back from an injury-hit season.",
    'Posch': "Aerial threat — stands in the air well.",
}

MANAGER_INSIGHTS = {
    'Eta': "Marie-Louise Eta, first woman head coach in Bundesliga history; took over from Urs Fischer.",
    'Fischer': "Urs Fischer, former Union manager — title-winning, contribution will never be forgotten.",
}

STYLE_EXAMPLES = [
    "Khedira leading that particular charge.",
    "At first sight, I'm with the referee.",
    "I think it's a race to the ball.",
    "Veratschnig venture forward. Here's Kohr.",
    "The hosts have been using the right-hand side, but they've elected to attack from the left.",
    "Tall striker. Hasn't had things go his way too much in this campaign.",
    "Here at the Mewa Arena.",
    "Wide of the target.",
    "Yeah, home form is always vital, especially for smaller clubs.",
    "Trying to keep Juranovic in check.",
    "He has to be content with a corner.",
    "We're inside the final fifteen here.",
    "Burke given a lengthy run, a feature of Eta's matches.",
]


def _parse_roster_text(text):
    out = []
    team = None
    role = 'starter'
    for line in text.splitlines():
        l = line.strip()
        if not l: continue
        if l.endswith('— home:'): team='FSV Mainz'; role='starter'; continue
        if l.endswith('— away:'): team='Union Berlin'; role='starter'; continue
        if 'Starting XI' in l: role='starter'; continue
        if 'Substitutes' in l: role='bench'; continue
        m = re.match(r'#(\S+)\s+(.+)', l)
        if m and team:
            full = m.group(2).strip()
            short = full.split(',')[0].strip() if ',' in full else full
            out.append({'team': team, 'number': m.group(1), 'short_name': short, 'name': full, 'role': role})
    return out


def _load_sr_positions():
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    out = {}
    for c in sr['lineups']['lineups']['competitors']:
        for p in c.get('players', []):
            out[p.get('name', '')] = p.get('position', '').replace('_', ' ')
    return out


def _load_team_aliases():
    """Tiny YAML parser sufficient for this schema (avoids adding pyyaml dep)."""
    path = Path('/home/ubuntu/commentary/match_data/m05_uni_md33/team_aliases.yaml')
    if not path.exists():
        return None
    out = {}
    current_team = None
    current_section = None
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'): continue
        if not raw.startswith(' '):  # top-level key
            current_team = raw.rstrip(':').strip()
            out[current_team] = {'aliases': {}}
            current_section = None
            continue
        if raw.startswith('  ') and not raw.startswith('   '):
            # 2-space indent
            k, _, v = raw.strip().partition(':')
            v = v.strip()
            if k == 'aliases':
                current_section = 'aliases'
            elif v:
                out[current_team][k.strip()] = v.strip(' "')
            else:
                current_section = k.strip()
        elif raw.startswith('    ') and not raw.startswith('     '):
            # 4-space indent: sub-category under aliases
            k, _, _ = raw.strip().partition(':')
            current_section = k.strip()
            out[current_team]['aliases'][current_section] = []
        elif raw.startswith('      - '):
            item = raw.strip()[2:].strip()
            out[current_team]['aliases'][current_section].append(item)
    return out


def build_match_context():
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
        'storyline': "Both sides safe in the table but jockeying for league position. Single commentator with analyst booth.",
        'final_score_private': 'M05 1 - 3 UNI',
    }


def _format_roster_block(roster, team_name, abbr):
    rows = [p for p in roster if p['team'] == team_name]
    if not rows: return ''
    lines = [f"{abbr} squad:"]
    for p in rows:
        bits = [f"#{p['number']}", p['short_name']]
        if p['name'] != p['short_name']:
            bits.append(f"({p['name']})")
        meta = [p['role']]
        if p['position']: meta.append(p['position'])
        bits.append(f"[{'/'.join(meta)}]")
        line = "  " + " ".join(bits)
        if p['insight']:
            line += f"\n    pre-game note: {p['insight']}"
        lines.append(line)
    return "\n".join(lines)


def _format_alias_block(aliases):
    if not aliases: return ''
    out = ['TEAM ALIASES — use these to vary how you refer to each team:']
    for team_key, data in aliases.items():
        label = data.get('full_name', team_key) + f" ({data.get('short', team_key)})"
        out.append(f"  {label}")
        for cat, items in data['aliases'].items():
            if items:
                out.append(f"    {cat}: " + ", ".join(items))
    return "\n".join(out)


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


def build_visual_prompt(ctx_text, latest_time_s, previous_calls, alias_usage_summary):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none yet"
    examples = "\n".join(f"  - \"{e}\"" for e in STYLE_EXAMPLES)
    return f"""You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers.

PROFILE: experienced English-language sportscaster. Short, sharp, urgent during
attacks; reflective during lulls; restrained when the picture is unclear.

VIDEO CONTEXT
Current video clock (relative to start of this slice): {latest_time_s:.1f}s.
You see a short burst of frames, oldest first, newest last. Comment on
whatever is happening in the NEWEST frame.

OUTPUT
- 3-12 words, usually one sentence or a fragment.
- Variety: rotate through action calls, named identifications, pass-type
  observations, tactical reads, time references, atmosphere, terse outcomes,
  player-context asides (only when you can use a pre-game note).
- Empty silence is fine. Return "NO_CALL" for unreadable / static / replay /
  pure-crowd shots, AND when nothing meaningful has changed since the last
  call (a skilled commentator goes quiet during routine possession; speech
  is reserved for moments of consequence).

NAMING — LEAN INTO IT
- NAME PLAYERS WHENEVER REASONABLE. Strong identification is the heart of
  good football commentary.
- Use the shirt number on the player's back when visible. Use kit colour +
  position + roster to pick the most likely name when not.
- Occasional misidentifications are acceptable — far less damaging than
  every line being "the Mainz striker / the Union winger".
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT carry forward a name from earlier frames — the player may have
  been substituted. Re-identify each call.
- DO NOT invent names that are not on either squad's roster.

TEAM-NAME VARIETY — STRICT RULE
- HEAR THIS: the previous lines have been hammering team names. Repeating
  "Mainz" or "Union" line after line sounds robotic. STOP doing this.
- If a team's short name appears in the recent calls below, you MUST use
  a DIFFERENT alias this turn (role token, nickname, kit colour, manager
  possessive, or place name).
- Even better: refer to the team IMPLICITLY by naming a player. "Burke
  driving wide" already tells viewers it's Union — you don't have to say
  "Union" too.
- Acceptable per line: at most ONE explicit team reference. Two is too
  many. Zero is often best.
- Check the "TEAM ALIAS USAGE" block below — it lists the aliases you must
  AVOID this turn and which alternatives are still fresh.

SCORELINE RULE
- DO NOT state the scoreline in your commentary. The score graphic is
  permanently on screen — viewers already see it.
- The ONLY exceptions: (a) something just changed the score in this very
  burst (a goal, a goal disallowed); (b) you're inside the final five
  minutes and want to mention the score for tension. Otherwise: silence on
  the score.

SUBSTITUTION RECOGNITION
- The fourth official holds up an electronic board with TWO numbers:
  RED (top) = player coming OFF; GREEN (bottom) = player coming ON.
- If you see that board, do not read it as a clock or a score. State it as
  a substitution and name BOTH players by matching the numbers to the
  rosters above.
- Example wording: "Becker comes off for Kohr." NOT "the referee shows
  20:44." NOT "fourth official holds 20:44."

SET-PIECE TEAM ATTRIBUTION
- For throw-ins, free kicks, corners, goal kicks: only name a team when you
  can clearly see which side picks up the ball, places it, or stands over it.
  Check the player's shirt number against the rosters to confirm which team.
- If the team is not clear from a single visible identifier, describe the
  set piece WITHOUT naming a team: "a throw on the left", "free kick near
  the corner".
- DO NOT carry forward team attribution from the previous burst — possession
  often switches at set pieces.

USING PRE-GAME NOTES
- The squad block below lists pre-game notes for some players. If a NAMED
  player has a note that fits the current moment, briefly weave it in.
- Do not use a note more than once per session.
- Do not invent notes for players who don't have them.

NO INVENTION
- No goals, cards, scores, substitutions, or events not visibly happening.
- The known final result is private metadata — do not reference it.

STYLE EXAMPLES (illustrative; do NOT transcribe verbatim):
{examples}

CONTEXT
{ctx_text}

RECENT CALLS — do not repeat the same observation:
{previous}

TEAM ALIAS USAGE IN LAST 6 LINES — avoid overusing these:
{alias_usage_summary}

Produce your next call:
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, burst_paths, latest_time_s, previous_calls, ctx_text, alias_usage_summary):
    content = [{"type": "input_text",
                "text": build_visual_prompt(ctx_text, latest_time_s, previous_calls, alias_usage_summary)}]
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


def summarise_alias_usage(recent_texts, aliases):
    """Produce a HARD instruction the model can follow this turn.

    Counts plain team-name usage in the last 3 accepted lines. If "Mainz"
    or "Union" was used in any of the last 3 lines, BAN that short name
    this turn and prescribe specific alternative aliases from the bank.
    """
    if not aliases:
        return "  (no alias config loaded)"

    last3 = recent_texts[-3:]
    blob = " ".join(last3).lower()

    out = []
    for team_key, data in aliases.items():
        short = data.get('short', team_key)
        short_count = blob.count(short.lower())
        # Find alternatives that have NOT been used recently
        alts_used = []
        alts_fresh = []
        for cat, items in data['aliases'].items():
            for alias in items:
                if alias.lower() == short.lower():
                    continue
                if alias.lower() in blob:
                    alts_used.append(alias)
                else:
                    alts_fresh.append(alias)
        if short_count >= 1:
            picks = ", ".join(alts_fresh[:4]) or "(any alternative from the bank)"
            out.append(f"  - {short!r} already used in last 3 lines. If you need to refer to that team this turn, USE ONE OF: {picks}. Do NOT say {short!r} again.")
        elif short_count == 0:
            out.append(f"  - {short!r} not used recently — OK to say {short!r} this turn if needed.")
    return "\n".join(out)


NO_CALL_RE = re.compile(r"^\s*(no[_ ]?call|n/a|—|-+)\s*$", re.I)
def is_no_call(text): return bool(NO_CALL_RE.match(text or ''))
def is_repetitive(text, previous, threshold):
    if not text: return False
    nt = set(re.sub(r'\W+', ' ', text.lower()).split())
    for prev in previous[-8:]:
        np = set(re.sub(r'\W+', ' ', prev.lower()).split())
        if not nt or not np: continue
        if len(nt & np) / len(nt | np) >= threshold:
            return True
    return False


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Frames: {len(frame_paths)}")
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    aliases = ctx['aliases']
    print(f"Context size: {len(ctx_text)} chars; aliases loaded: {bool(aliases)}")
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    booth_busy_until = 0.0
    LIVE_GAP_S = 1.20  # real commentator pauses between phrases
    no_call=0; repetitive=0; errors=0; skipped=0

    t_start = time.time()
    last_print = time.time()
    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        # booth_busy_until is measured in game (video) time + natural lag, so
        # that lines stay sequential at playback time.
        if latest_time_s < booth_busy_until - NATURAL_LAG_S + LIVE_GAP_S:
            skipped += 1
            continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        alias_usage = summarise_alias_usage(prev_texts, aliases)
        text, vision_ms, err = call_vision(client, burst, latest_time_s, prev_texts, ctx_text, alias_usage)
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
        elif is_repetitive(text, [a['text'] for a in accepted], DEDUP_JACCARD):
            repetitive += 1; attempt['reason']='repetitive'
        else:
            # Estimate spoken duration for booth-busy reservation. Real TTS
            # comes later — assume ~3 words/sec for live-pace planning.
            words = len(text.split())
            est_duration_s = max(1.2, words / 3.0)
            scheduled_start_s = latest_time_s + NATURAL_LAG_S
            scheduled_end_s = scheduled_start_s + est_duration_s
            attempt.update({
                'accepted': True,
                'est_duration_s': round(est_duration_s, 3),
                'scheduled_start_s': round(scheduled_start_s, 3),
                'scheduled_end_s': round(scheduled_end_s, 3),
            })
            accepted.append(attempt)
            booth_busy_until = scheduled_end_s
        all_attempts.append(attempt)
        if time.time() - last_print > 10:
            print(f"  burst {burst_idx}/{len(bursts)} t={latest_time_s:.1f}s accepted={len(accepted)} skipped={skipped} elapsed={time.time()-t_start:.0f}s last={(text or '')[:60]!r}")
            last_print = time.time()

    print(f"\nSummary: vision={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors}")
    if accepted:
        lats_v = sorted(a['vision_latency_ms'] for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision p50={pct(lats_v,0.5)}ms p90={pct(lats_v,0.9)}ms")
    print(f"Wall time: {time.time()-t_start:.0f}s")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts:
            f.write(json.dumps(a) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted:
            f.write(json.dumps(a) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# AI commentary v4 — alias rotation + 5 new rules\n# {len(accepted)} accepted of {len(all_attempts)} calls\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")
    print(f"Wrote {OUT_JSONL}, {OUT_SCHED}, {OUT_KEPT}")


if __name__ == '__main__':
    main()

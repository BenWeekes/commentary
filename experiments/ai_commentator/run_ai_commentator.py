#!/usr/bin/env python3
"""AI commentator over a 5-minute football clip — worldcupvoice-style.

Offline batch (no Agora, no RTMP). Reads pre-extracted JPEG frames, walks a
sliding 4-frame burst at 0.55s pace, calls gpt-5.4-mini vision per burst with
a worldcupvoice-derived prompt that includes the full roster + positions +
formations from sr_cache.json, accepts non-NO_CALL non-repetitive responses,
and writes:

  - commentary.jsonl   (one row per burst: t, latency, model output, accepted)
  - commentary_kept.txt   (final commentary, timestamped, STT-style)
"""
from __future__ import annotations
import base64, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Manual env load (no python-dotenv on path)
for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
# Override with user-provided service-account key for this run

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary.jsonl'
OUT_KEPT = BASE / 'commentary_kept.txt'

# worldcupvoice defaults
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.4-mini'
MAX_OUTPUT_TOKENS = 40
TEMPERATURE = 0.55
WINDOW_OFFSET_S = 300.0  # source slice begins at video time 5:00 of the master

# ---- player roster + positions ----------------------------------------------

def _parse_roster_text(text):
    """Parse roster.json text format → list of (team, short_name, full_name, number, role)."""
    out = []
    team = None
    role = 'starter'
    for line in text.splitlines():
        l = line.strip()
        if not l: continue
        if l.endswith('— home:') or l.endswith('— away:'):
            team = l.split(' (')[0].strip()
            role = 'starter'
            continue
        if 'Starting XI' in l: role = 'starter'; continue
        if 'Substitutes' in l: role = 'bench'; continue
        m = re.match(r'#(\S+)\s+(.+)', l)
        if m and team:
            number = m.group(1)
            full = m.group(2).strip()
            short = full.split(',')[0].strip() if ',' in full else full
            out.append({'team': team, 'number': number, 'short_name': short, 'name': full, 'role': role})
    return out


def _load_sr_positions():
    """name → human-readable position string from sr_cache."""
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    out = {}
    for c in sr['lineups']['lineups']['competitors']:
        for p in c.get('players', []):
            out[p.get('name', '')] = p.get('position', '').replace('_', ' ')
    return out


def build_match_context():
    roster_text = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/roster.json'))['roster_text']
    roster = _parse_roster_text(roster_text)
    positions = _load_sr_positions()
    for p in roster:
        p['position'] = positions.get(p['name'], '')

    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    formations = {}
    for c in sr['lineups']['lineups']['competitors']:
        formations[c['name']] = c.get('formation', {}).get('type', '?')

    return {
        'sport': 'football',
        'title': 'FSV Mainz vs Union Berlin (Bundesliga matchday 33)',
        'venue': 'Mewa Arena, Mainz',
        'home_team': 'FSV Mainz', 'home_abbr': 'M05',
        'away_team': 'Union Berlin', 'away_abbr': 'UNI',
        'home_color': 'predominantly white shirts with red trim',
        'away_color': 'predominantly red shirts',
        'home_formation': formations.get('FSV Mainz', '?'),
        'away_formation': formations.get('Union Berlin', '?'),
        'roster': roster,
        'storyline': (
            "Both sides safe in the table but jockeying for league position. "
            "Single commentator + analyst booth. Goals possible from Becker, Tietz, Burke, Juranovic."
        ),
        # Private — do not announce as live score
        'final_score_private': 'M05 1 - 3 UNI',
    }


def _format_roster_for_prompt(roster, team_name, abbr):
    rows = [p for p in roster if p['team'] == team_name]
    if not rows: return ''
    entries = []
    for p in rows:
        detail = f"#{p['number']} {p['short_name']}"
        if p['name'] != p['short_name']:
            detail += f" ({p['name']})"
        meta = [p['role']]
        if p['position']:
            meta.append(p['position'])
        detail += f" [{'/'.join(meta)}]"
        entries.append(detail)
    return f"{abbr} player map: " + "; ".join(entries) + "."


def build_match_context_text(ctx):
    lines = [
        f"Game context: live football, {ctx['title']}, at {ctx['venue']}.",
        f"Storyline: {ctx['storyline']}",
        f"{ctx['home_team']} ({ctx['home_abbr']}) formation: {ctx['home_formation']}, uniforms: {ctx['home_color']}.",
        f"{ctx['away_team']} ({ctx['away_abbr']}) formation: {ctx['away_formation']}, uniforms: {ctx['away_color']}.",
    ]
    rh = _format_roster_for_prompt(ctx['roster'], ctx['home_team'], ctx['home_abbr'])
    ra = _format_roster_for_prompt(ctx['roster'], ctx['away_team'], ctx['away_abbr'])
    if rh: lines.append(rh)
    if ra: lines.append(ra)
    lines.append(
        "Player identification rules: 1) inspect shirt numbers on the ball carrier, "
        "passer, shooter, goalkeeper, and nearest defender. 2) if the number, kit "
        "colour, and possession context match the roster, use the player's short "
        "name. 3) otherwise describe by role or position generically."
    )
    lines.append(
        f"Broadcast notes: 1) the known final result ({ctx['final_score_private']}) is "
        "private metadata, do not announce it. 2) do not say kick-off, goal, equaliser, "
        "card, or off-screen events unless the newest frame visibly supports it."
    )
    return "\n".join(lines)


# ---- worldcupvoice-style English visual prompt ------------------------------

STYLE_PROMPT = (
    "You are an energetic American English sportscaster. Call the newest visible "
    "action with sharp play-by-play cadence, urgency on attacks, and restraint "
    "when the picture is unclear."
)


def build_visual_prompt(ctx_text, latest_time_s, previous_calls):
    previous = "\n".join(f"- {c}" for c in previous_calls[-5:]) or "- none"
    return (
        "You are a live football play-by-play commentator, not an image captioner.\n"
        "Commentator profile: English Sportscaster.\n"
        f"Style guide: {STYLE_PROMPT}\n"
        f"{ctx_text}\n"
        f"Current video clock in the source: {latest_time_s:.1f}s.\n"
        "You are given a short burst of frames, oldest first and newest last. "
        "Call the newest visible live action: ball movement, dribble, pass, cross, "
        "shot, save, clearance, press, counterattack, defensive line, celebration, "
        "crowd surge, or players organizing for the next phase. Use natural live broadcast "
        "cadence: short when the action is fast, longer when the play is developing, usually "
        "4 to 16 words, one sentence max. It is okay to sound clipped, urgent, or mid-play.\n"
        "Default to a grounded call when a live game, players, pitch, or "
        "ball-side action is visible. Return exactly NO_CALL only when the newest "
        "frame is not readable, no football action is visible, or the scene is clearly "
        "a static timeout/replay/crowd-only shot with no new visible change.\n"
        "Before writing, inspect visible shirt numbers on the ball carrier, "
        "passer, crosser, shooter, goalkeeper, and nearest defender. Naming priority: "
        "if a shirt number is readable and the team kit matches the roster map, use "
        "that player's short name instead of a generic role. If the number is not "
        "readable, fall back to a generic role.\n"
        "Do not invent player names, fouls, scores, or off-screen events.\n"
        "Recent calls to avoid repeating:\n"
        f"{previous}"
    )


# ---- vision call -----------------------------------------------------------

def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, burst_paths, latest_time_s, previous_calls, ctx_text):
    """One vision call on a burst of frame paths. Returns (text, latency_ms, error_str)."""
    content = [
        {"type": "input_text", "text": build_visual_prompt(ctx_text, latest_time_s, previous_calls)},
    ]
    for p in burst_paths:
        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}",
        })

    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        text = (resp.output_text or '').strip()
        return text, latency_ms, None
    except Exception as e:
        return None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {str(e)[:200]}"


# ---- dedup / acceptance ----------------------------------------------------

_NO_CALL_RE = re.compile(r"^\s*(no[_ ]?call|n/a|—|-+)\s*$", re.I)

def is_no_call(text):
    return bool(_NO_CALL_RE.match(text or ''))

def is_repetitive(text, previous, threshold=0.7):
    if not text: return False
    nt = re.sub(r'\W+', ' ', text.lower()).strip()
    for prev in previous[-8:]:
        np = re.sub(r'\W+', ' ', prev.lower()).strip()
        # token-overlap Jaccard
        a, b = set(nt.split()), set(np.split())
        if not a or not b: continue
        j = len(a & b) / len(a | b)
        if j >= threshold:
            return True
    return False


# ---- main loop -------------------------------------------------------------

def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Frames: {len(frame_paths)}  (expected ~{int(300/SAMPLE_INTERVAL_S)})")

    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    print(f"\nRoster: {len(ctx['roster'])} players ({sum(1 for p in ctx['roster'] if p['position'])} with positions)")
    print(f"Match context text size: {len(ctx_text)} chars\n")

    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    # bursts: i is the index of the NEWEST frame in this burst (0-based)
    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        burst = frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]
        latest_time_s = (i + 1) * SAMPLE_INTERVAL_S  # video time of newest frame in slice
        bursts.append((i, latest_time_s, burst))
    print(f"Bursts to process: {len(bursts)}  ({CONTEXT_FRAMES} frames each, latest @ +0.55s steps)")

    # Parallel up to 5 concurrent vision calls — but accept results IN ORDER so
    # the dedup uses real preceding accepted commentary.
    # Strategy: submit a sliding window of futures, harvest oldest first.
    accepted_calls = []  # list of text strings, used for "previous_calls" + dedup
    all_rows = []        # all bursts with their outcome, written to jsonl
    futures = {}         # i -> Future
    pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix='vision')

    def submit(i, latest_time_s, burst):
        # Snapshot previous_calls AT SUBMIT TIME — small staleness but OK
        prev_snapshot = list(accepted_calls[-5:])
        return pool.submit(call_vision, client, burst, latest_time_s, prev_snapshot, ctx_text)

    # Pre-fill the window
    WINDOW = 5
    next_to_submit = 0
    next_to_harvest = 0
    while next_to_submit < min(WINDOW, len(bursts)):
        i, t, b = bursts[next_to_submit]
        futures[i] = submit(i, t, b)
        next_to_submit += 1

    t_start = time.time()
    while next_to_harvest < len(bursts):
        i, latest_time_s, burst = bursts[next_to_harvest]
        fut = futures.pop(i)
        text, latency_ms, err = fut.result()
        accepted = False
        reason = None
        if err:
            reason = 'error'
        elif text is None or text == '':
            reason = 'empty'
        elif is_no_call(text):
            reason = 'no_call'
        elif is_repetitive(text, accepted_calls):
            reason = 'repetitive'
        else:
            accepted = True
            accepted_calls.append(text)
        row = {
            'burst_index': i,
            'newest_frame': burst[-1].name,
            'video_time_s': round(latest_time_s, 2),
            'master_time_s': round(latest_time_s + WINDOW_OFFSET_S, 2),
            'model': MODEL,
            'vision_latency_ms': latency_ms,
            'text': text,
            'accepted': accepted,
            'reason': reason,
            'error': err,
        }
        all_rows.append(row)
        if next_to_harvest % 25 == 0:
            elapsed = time.time() - t_start
            print(f"  [{next_to_harvest}/{len(bursts)}] elapsed={elapsed:.0f}s "
                  f"accepted={len(accepted_calls)} "
                  f"last_latency={latency_ms}ms last_text={text!r}")
        next_to_harvest += 1
        # Submit next
        if next_to_submit < len(bursts):
            i2, t2, b2 = bursts[next_to_submit]
            futures[i2] = submit(i2, t2, b2)
            next_to_submit += 1

    pool.shutdown(wait=True)

    # Write outputs
    with open(OUT_JSONL, 'w') as f:
        for row in all_rows:
            f.write(json.dumps(row) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write("# AI commentary (worldcupvoice-style, gpt-5.4-mini)\n")
        f.write(f"# Source: slice_5min.mp4 of m05_uni_eval_25min, offset {WINDOW_OFFSET_S:.0f}s\n")
        f.write(f"# {len(accepted_calls)} accepted of {len(all_rows)} bursts\n\n")
        for row in all_rows:
            if row['accepted']:
                f.write(f"[{row['video_time_s']:7.2f}s] {row['text']}\n")

    # Summary
    print(f"\n=== Summary ===")
    print(f"Bursts processed: {len(all_rows)}")
    print(f"Accepted: {len(accepted_calls)}")
    reasons = {}
    for row in all_rows:
        if not row['accepted']:
            reasons[row['reason']] = reasons.get(row['reason'], 0) + 1
    print(f"Rejected by reason: {reasons}")
    lats = sorted(r['vision_latency_ms'] for r in all_rows if r['vision_latency_ms'])
    if lats:
        print(f"Vision latency: p50={lats[len(lats)//2]}ms p90={lats[int(len(lats)*0.9)]}ms max={max(lats)}ms")
    print(f"Total wall time: {time.time()-t_start:.1f}s")
    print(f"\nSaved: {OUT_JSONL.name}, {OUT_KEPT.name}")


if __name__ == '__main__':
    main()

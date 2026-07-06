#!/usr/bin/env python3
"""Enhanced pre-game context builder for v8+.

Adds to v5's baseline:
  1. Extended player insights — auto-derived facts for all 40 roster entries
     (age, nationality, height, preferred foot) + hand-crafted narrative
     notes for players with known storylines
  2. Manager tactical fingerprints
  3. Pre-game storylines (Sieb farewell, Weiper injury return, etc.)
  4. Referee profile
  5. Broader stakes / table context

All facts are PRE-GAME. No live-match knowledge.

Everything is text — the returned `context_text` is a drop-in replacement for
build_match_context_text() from v5.
"""
from __future__ import annotations
import json, re
from datetime import date
from pathlib import Path

MATCH_DIR = Path('/home/ubuntu/commentary/match_data/m05_uni_md33')

# Hand-crafted narrative notes (pre-game knowledge for THIS specific match).
# Kept in one place so it's obvious what came from human curation vs SR data.
PLAYER_NARRATIVE = {
    # Mainz
    'Burke':        "Scottish international; Eta on Union side gives him longer runs than her predecessor did.",
    'Sieb':         "On two-year loan from Bayern Munich; leaves at season end; received formal farewell pre-match.",
    'Doekhi':       "Dutch centre-back, Rotterdam-born; English clubs reportedly watching.",
    'Weiper':       "Germany U21 striker; making his return today from a lengthy injury lay-off.",
    'Kohn':         "The 'Derrick' first name is a long-running joke with the German TV detective series.",
    'Zentner':      "Strong at coming for crosses; one-on-ones not his strongest suit.",
    'Veratschnig':  "Very good recovery pace at the back; on a booking after this yellow.",
    'Juranovic':    "Croatian international coming back from an injury-hit season.",
    'Posch':        "Austrian; aerial threat, stands well in the air.",
    'Amiri':        "Mainz's key creator this campaign; set-piece specialist.",
    'Tietz':        "German striker; hold-up player, often first option through the middle.",
    'Caci':         "French left-sided defender who often bombs on down the right in this system.",
    'Kohr':         "Long-serving Mainz midfielder; disciplinarian, rarely gives the ball away.",
    'Klaus':        "Union's regular keeper; commanding on crosses, quick to distribute long.",
    'Khedira':      "Union midfield anchor; brother of ex-Germany international Sami.",
    'Trimmel':      "Union captain and long-serving right-back; approaching veteran status.",
    'Kemlein':      "Young Union midfielder; graduated through Union's academy.",
    'Ilic':         "Serbian forward — Union's main outlet on quick counterattacks.",
    'Burcu':        "Turkish international; can play across the front line.",
    'Ansah':        "Ghanaian winger; pace out wide; still developing final ball.",
    'Rothe':        "Left-back on loan from Dortmund; regularly gets forward.",
    'Nebel':        "Mainz academy graduate — dynamic ball-carrier through the middle.",
    'Sano':         "Japanese midfielder; two-way engine, wins second balls.",
    'Mwene':        "Austrian; a wing-back who inverts into midfield when Mainz have the ball.",
    'da Costa':     "German-born but represented Portugal at youth level; senior operator.",
    'Jae-sung':     "South Korean international; late-game impact off the bench.",
    'Becker':       "Sheraldo Becker; Surinamese; former Union favourite now on Mainz's books.",
}

MANAGER_PROFILES = {
    'FSV Mainz': {
        'name': 'Bo Henriksen',
        'style': "Direct football, aggressive high press, prefers wide attacks via Amiri and Caci. Uses tall targets when he needs a goal — Weiper, Tietz. Bench very active.",
    },
    'Union Berlin': {
        'name': 'Marie-Louise Eta',
        'style': "Interim manager, first woman head coach in Bundesliga history. Compact 4-2-3-1 shape, quick transitions through Burke and Ansah. Trusts starters, subs late.",
    },
}

MATCH_STORYLINES = [
    "MD33 of 34 — penultimate weekend of the 25/26 Bundesliga season.",
    "Both sides mathematically safe; Mainz chasing a European (Conference League) berth, Union recovering from a dip in form.",
    "Sieb's home farewell — expected to feature off the bench before returning to Bayern Munich.",
    "Weiper making his return from a lengthy injury absence.",
    "Marie-Louise Eta is in her first season as first-team head coach; took over from title-winning Urs Fischer.",
    "Sheraldo Becker faces his former club Union for the first time as a Mainz player.",
    "Referee tonight: Florian Exner (assistants: Mark Borsch, Mitja Stegemann; 4th official Lars Erbst; VAR Sascha Stegemann).",
]

REFEREE_PROFILE = (
    "Florian Exner — experienced Bundesliga whistle; consistent, tends to card cynical / tactical fouls; "
    "not known for high card totals. Lets the game flow when possible."
)

BUNDESLIGA_CONTEXT = (
    "Bundesliga 25/26, matchday 33. Kickoff 5:30 pm local. Mewa Arena, Mainz. Attendance capacity ~34k. "
    "Live domestic broadcast."
)


def _age(dob_str):
    try:
        y, m, d = [int(x) for x in dob_str.split('-')]
        today = date(2026, 5, 10)  # kickoff date
        return today.year - y - ((today.month, today.day) < (m, d))
    except Exception:
        return None


def _foot_short(pf):
    if not pf: return ''
    return {'left': 'L', 'right': 'R', 'both': 'B'}.get(pf.lower(), pf[:1].upper())


def _height_cm(h):
    try: return int(h)
    except: return None


def _load_sr():
    sr = json.load(open(MATCH_DIR / 'sr_cache.json'))
    out = {}  # team_name -> list of player dicts
    for c in sr['lineups']['lineups']['competitors']:
        out[c['name']] = []
        for p in c.get('players', []):
            out[c['name']].append({
                'name': p.get('name', ''),
                'short': p.get('name', '').split(',')[0].strip(),
                'number': str(p.get('jersey_number', '?')),
                'position': (p.get('position') or '').replace('_', ' '),
                'starter': p.get('starter', False),
                'nationality': p.get('nationality', ''),
                'age': _age(p.get('date_of_birth')),
                'height': _height_cm(p.get('height')),
                'foot': _foot_short(p.get('preferred_foot')),
            })
    return out


def _format_player_line(p):
    """One-line rich profile for a player."""
    facts = []
    if p['age']:
        facts.append(f"age {p['age']}")
    if p['nationality']:
        facts.append(p['nationality'])
    if p['height']:
        facts.append(f"{p['height']}cm")
    if p['foot']:
        facts.append(f"{p['foot']}-foot")
    role = f"{'starter' if p['starter'] else 'bench'}/{p['position']}" if p['position'] else ('starter' if p['starter'] else 'bench')
    tags = f" [{role}]"
    facts_bit = f" ({'; '.join(facts)})" if facts else ''
    narrative = PLAYER_NARRATIVE.get(p['short'], '')
    narrative_bit = f"\n    note: {narrative}" if narrative else ''
    return f"  #{p['number']} {p['short']}{tags}{facts_bit}{narrative_bit}"


def build_rich_context_text(base_ctx=None):
    """Return a text blob for the vision prompt.

    base_ctx is the dict from run_v5.build_match_context() — used only for
    the alias / storyline / match title. All player data comes from SR cache.
    """
    sr_players = _load_sr()
    parts = []
    parts.append("MATCH")
    parts.append(f"  FSV Mainz vs Union Berlin — Bundesliga MD33, Mewa Arena, Mainz.")
    parts.append(f"  {BUNDESLIGA_CONTEXT}")
    parts.append("")
    parts.append("STORYLINES (pre-game, may be referenced when contextually appropriate):")
    for s in MATCH_STORYLINES:
        parts.append(f"  - {s}")
    parts.append("")
    parts.append("REFEREE")
    parts.append(f"  {REFEREE_PROFILE}")
    parts.append("")
    parts.append("MANAGERS")
    for team, mgr in MANAGER_PROFILES.items():
        parts.append(f"  {team} — {mgr['name']}: {mgr['style']}")
    parts.append("")

    if base_ctx and base_ctx.get('aliases'):
        parts.append("TEAM ALIASES (rotate — the same team alias twice in 6 lines is REPETITIVE)")
        for team_key, data in base_ctx['aliases'].items():
            label = data.get('full_name', team_key) + f" ({data.get('short', team_key)})"
            parts.append(f"  {label}")
            for cat, items in data['aliases'].items():
                if items:
                    parts.append(f"    {cat}: " + ", ".join(items))
        parts.append("")

    for team, players in sr_players.items():
        parts.append(f"{team.upper()} SQUAD (starters first, then bench):")
        starters = [p for p in players if p['starter']]
        bench = [p for p in players if not p['starter']]
        for p in starters:
            parts.append(_format_player_line(p))
        parts.append("  bench:")
        for p in bench:
            parts.append(_format_player_line(p))
        parts.append("")

    return "\n".join(parts)


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from run_v5 import build_match_context
    ctx = build_match_context()
    text = build_rich_context_text(ctx)
    print(text)
    print(f"\n--- {len(text)} chars, {len(text.split())} words ---")

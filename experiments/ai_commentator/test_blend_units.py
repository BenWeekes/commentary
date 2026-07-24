#!/usr/bin/env python3
"""Unit tests for blend-pipeline invariants (roster resolution, attribution guard,
clock grounding, gate fixtures). Plain asserts — run with:
  BLEND_DELAY_S=6 .venv/bin/python test_blend_units.py
Exit code 0 = all pass. Added after the 2026-07-24 codex review (no automated tests
existed for roster resolution, timing, or gate behaviour)."""
import os, sys

os.environ.setdefault('BLEND_DELAY_S', '6')
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')

import run_blend_live as B
import run_blend_true_live as T
import eval_snapshot as E

FAILS = []


def check(name, cond, detail=''):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


print("== roster resolution (codex #3: shirt numbers collide across teams) ==")
check("all 40 players kept for surname grounding", len(B.ALL_PLAYERS) == 40, len(B.ALL_PLAYERS))
check("colliding number needs a team", B.player_by_number(11) is None)
check("(Mainz, 11) -> Sieb", (B.player_by_number(11, 'Mainz') or {}).get('name') == 'Sieb')
check("(Union, 11) -> Woo-yeong", (B.player_by_number(11, 'Union') or {}).get('name') == 'Woo-yeong')
check("within-team duplicate fails closed", B.player_by_number(13, 'Union') is None)
_unique = [n for n, tms in B.NUM_TEAMS.items()
           if len(tms) == 1 and (next(iter(tms)), n) not in B._AMBIG]
check("unique number resolves team-less",
      len(_unique) > 0 and all(B.player_by_number(n) is not None for n in _unique),
      f"{len(_unique)} unique numbers")
check("guard covers every surname", len(T.SUR2TEAM) == 40, len(T.SUR2TEAM))

print("== enforce_attribution (R12 guard) ==")
cases = [
    ("Kohn goes into the book for Mainz.", "Kohn goes into the book for Union."),
    ("Kohn sees yellow for Union.", None),                       # correct -> untouched
    ("Kohn booked; free kick for Mainz.", None),                 # award beneficiary
    ("Mainz have pushed for ten minutes.", None),                # duration 'for'
    ("Kohn fouls Amiri, free kick for Mainz.", None),            # both teams named
    ("Kohn steps forward for the home side.", "Kohn steps forward for the away side."),
    # broadened constructions (codex-2 #3): wrong team in ANY position on card/goal/sub lines
    ("Yellow for Kohn, Mainz booked.", "Yellow for Kohn, Union booked."),
    ("Kohn is booked and Mainz are down to a nervy spell.", "Kohn is booked and Union are down to a nervy spell."),
    ("Mainz substitute: Sieb and Weiper involved.", None),   # Sieb IS Mainz — untouched
]
for src, want in cases:
    out = T.enforce_attribution(src)
    check(f"guard: {src[:38]!r}", out == (want if want else src), f"got {out!r}")

print("== match clock grounding (reviewer-flagged drift) ==")
check("t=0 ~77th min", "76th" in B.match_clock_at(0) or "77th" in B.match_clock_at(0))
check("t=241 ~9-10 min left", "9 minutes" in B.match_clock_at(240.9) or "10 minutes" in B.match_clock_at(240.9),
      B.match_clock_at(240.9))

print("== gate fixtures (codex #8: false-pass modes) ==")
mk = lambda t, txt, **kw: dict({'src': 'blend', 'text': txt, 'video_time_s': t,
                                'fr': '', 'pt': ''}, **kw)
# R3: 'Union' alone must NOT count as a player name on a repeated event
b = [mk(10.0, "Free kick for Union.", vision='event: free_kick (Union)'),
     mk(20.0, "Union free kick again here.", vision='event: free_kick (Union)')]
fx = E.run_fixtures(b, '/missing.jsonl')
check("R3: bare team name is not a player name", fx['R3'] is False, fx['R3'])
b2 = [mk(10.0, "Free kick for Union.", vision='event: free_kick (Union)'),
      mk(20.0, "Khedira over the free kick.", vision='event: free_kick (Union)')]
check("R3: roster surname satisfies new-info", E.run_fixtures(b2, '/x.jsonl')['R3'] is True)
# R4: alias flip without transition must fail
b3 = [mk(10.0, "The hosts keep the ball nicely."),
      mk(15.0, "The visitors keep it now.")]
check("R4: alias flip without marker fails", E.run_fixtures(b3, '/x.jsonl')['R4'] is False)
b4 = [mk(10.0, "The hosts keep the ball nicely."),
      mk(15.0, "The visitors win it back and keep it.")]
check("R4: marked flip passes", E.run_fixtures(b4, '/x.jsonl')['R4'] is True)
# R12/R13 canaries
b5 = [mk(10.0, "Kohn sees yellow for Mainz.")]
check("R12 fixture catches wrong team", E.run_fixtures(b5, '/x.jsonl')['R12'] is False)
b6 = [mk(10.0, "Kohn, Burke and Klaus all in the frame.")]
check("R13 fixture catches camera line", E.run_fixtures(b6, '/x.jsonl')['R13'] is False)
b7 = [mk(10.0, "Yellow for Kohn, Mainz booked.")]
check("R12 fixture catches broadened construction", E.run_fixtures(b7, '/x.jsonl')['R12'] is False)
b8 = [mk(10.0, "Kohn booked; free kick for Mainz.")]
check("R12 fixture allows award beneficiary", E.run_fixtures(b8, '/x.jsonl')['R12'] is True)

print("== worst-of-N fail-closed (codex #8) ==")
check("missing survival counts as 0.0", E.WORST['survival']([0.99, None]) == 0.0)
check("missing hallucinations counts as fail", E.WORST['hallucinations']([0, None]) == 999)

print("== localizers fail silent, never English (codex #7) ==")
import inspect
src_fr = inspect.getsource(B.translate_fr)
check("translate_fr returns None on failure", 'return None' in src_fr)
import pathlib
_TL_SRC = (pathlib.Path(__file__).parent / 'run_blend_true_live.py').read_text()
check("_fr raises on None (missing track)", "raise RuntimeError('fr translate failed')" in _TL_SRC)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES: {FAILS}")
    sys.exit(1)
print("ALL UNIT CHECKS PASS")

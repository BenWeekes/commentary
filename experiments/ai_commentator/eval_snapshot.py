#!/usr/bin/env python3
"""Regression gate for the HITL tuning loop.

Snapshots every guarded + watched metric for a blend run into one JSON, and
compares two snapshots to give an ACCEPT / REJECT verdict for a candidate rule.

  # after a rerun with a candidate rule enabled:
  .venv/bin/python eval_snapshot.py snapshot commentary_blend_live_eager.jsonl > cand.json
  .venv/bin/python eval_snapshot.py compare baseline.json cand.json

Guarded (any regression -> REJECT): hallucinations=0, survival>=0.95,
desync shifts=0, first line <=2s. Watched (report only): words, gaps>=15s,
judge realism/variety, named-player lines.
Full workflow: docs/ai/L2/hitl_tuning_workflow.md
"""
import json, os, re, sys
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
sys.path.insert(0, str(BASE))


def snapshot(jsonl_path, skip_llm=False):
    b = sorted([json.loads(l) for l in open(jsonl_path) if l.strip()],
               key=lambda x: x['video_time_s'])
    ts = [x['video_time_s'] for x in b]
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    rep_file = Path(str(jsonl_path).replace('commentary_blend_live', 'latency_report')
                    .replace('.jsonl', '.json'))
    rep = json.loads(rep_file.read_text()) if rep_file.exists() else {}
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    sur = {p['name'].split(',')[0].strip() for c in sr['lineups']['lineups']['competitors']
           for p in c['players']}
    named = sum(1 for x in b if x['src'] == 'blend'
                and any(re.search(r'\b' + re.escape(s) + r'\b', x['text']) for s in sur))
    snap = {
        'file': str(jsonl_path), 'lines': len(b),
        'stt_lines': sum(1 for x in b if x['src'] == 'soniox'),
        'words': sum(len(x['text'].split()) for x in b),
        'survival': rep.get('survival_rate'),
        'desync_shifts_gt_1_5': sum(1 for x in b
                                    if x.get('lat', {}).get('audio_shift_s', 0) > 1.5),
        'first_line_s': ts[0] if ts else None,
        'max_gap_s': round(max(gaps), 1) if gaps else None,
        'gaps_ge_15s': sum(1 for g in gaps if g >= 15),
        'named_blend_lines': named,
        'fr_track_missing': sum(1 for x in b if 'fr' in (x.get('lat', {}).get('missing_tracks') or [])),
        'pt_track_missing': sum(1 for x in b if 'pt' in (x.get('lat', {}).get('missing_tracks') or [])),
    }
    det_path = str(jsonl_path).replace('commentary_blend_live', 'vis_detections').replace('.jsonl', '.jsonl')
    det_path = det_path.replace('vis_detections_eager', 'vis_detections_eager')
    snap['fixtures'] = run_fixtures(b, Path(str(jsonl_path).replace(
        'commentary_blend_live', 'vis_detections')))
    if not skip_llm:
        import judge as J
        from concurrent.futures import ThreadPoolExecutor
        # judge scope: lines that CLAIM an event (card/goal/sub/save/shot/corner/pen) —
        # a single-frame judge cannot validate multi-second possession dynamics (9/11
        # flags on grounded possession lines incl. a correctly-corrected card line);
        # possession grounding is enforced by the deterministic fixtures instead
        EVENT_CLAIM_RX = re.compile(
            r'yellow|red card|book(ed|ing)?\b|sent off|penalt|corner|free kick|\bsub\b|'
            r'substitut|\bsave[sd]?\b|\bshot\b|\bshoots\b|header|\bscor|\bgoal\b|VAR',
            re.I)
        gen = [x for x in b if x['src'] == 'blend' and EVENT_CLAIM_RX.search(x['text'])]
        with ThreadPoolExecutor(max_workers=6) as ex:
            vs = list(ex.map(lambda x: J.judge_line(x['text'], J.frame_for_time_s(x['video_time_s'])), gen))
        def _hval(v):
            # judge schema is numeric 0/1; tolerate bool/'true' variants; None = malformed
            if not isinstance(v, dict) or 'hallucination_likely' not in v:
                return None
            raw = v['hallucination_likely']
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, (int, float)):
                return int(raw != 0)
            if isinstance(raw, str) and raw.strip().lower() in ('0', '1', 'true', 'false'):
                return int(raw.strip().lower() in ('1', 'true'))
            return None
        hvals = [_hval(v) for v in vs]
        hall = sum(1 for h in hvals if h == 1)
        jfail = sum(1 for h in hvals if h is None)
        style = J.judge_run_style([x['text'] for x in b])
        snap['judge_failures'] = jfail
        # fail closed (codex-3): a judge outage must not read as zero hallucinations
        snap['hallucinations'] = None if jfail else hall
        snap['judge_realism'] = style.get('realism_1_5')
        snap['judge_variety'] = style.get('variety_1_5')
    return snap


PRIORITY_EVENTS = {'yellow_card', 'red_card', 'goal', 'penalty'}
# a GOAL CALL is scoring language — not any sentence containing 'goal' ('guards the
# goal', 'in goal', "Becker's goal" replay refs are not calls) (codex-4 #4)
GOAL_CALL_RX = re.compile(
    r'\bscor(?:es|ed)\b|\bgoal!|\bwhat a goal\b|\ba goal for\b|^goal for\b|\bfinds the net\b'
    r'|\bin the (?:back of the )?net\b|\bmakes it \d|\bhave scored\b', re.I)
FILLER_RX = re.compile(r'quiet spell|midfield battle continues|still all square', re.I)
FR_BANNED = ['sonder', 'dernier tiers', 'moment calme']
TRANSITION_RX = re.compile(r'win|won|regain|turn|steal|intercept|back|break|rob|force', re.I)
# team-reference aliases (R11 approved forms) -> canonical team, so R4 sees
# "the hosts keep it" -> Mainz (codex #8: literal Mainz/Union missed aliases)
FORM2TEAM = {}
for _team, _forms in {'Mainz': ['Mainz', 'FSV Mainz', 'the hosts', 'the home side', 'the reds'],
                      'Union': ['Union', 'Union Berlin', 'the visitors', 'the away side',
                                'the men in green']}.items():
    for _f in _forms:
        FORM2TEAM[_f.lower()] = _team
_FORMS_ALT = '|'.join(sorted((re.escape(f) for f in FORM2TEAM), key=len, reverse=True))
POSS_RX = re.compile(r'\b(' + _FORMS_ALT + r')\b.{0,30}\b'
                     r'(possess|keep|on the ball|have it|work|circulat|hold)', re.I)

def _poss_team(m):
    return FORM2TEAM.get(m.group(1).lower()) if m else None

# roster surnames for the R3 "named" check (codex #8: a bare capitalized word —
# including 'Union' — used to count as a player name)
_sr_names = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
_SURNAMES = {p['name'].split(',')[0].strip()
             for c in _sr_names['lineups']['lineups']['competitors'] for p in c['players']
             if len(p.get('name', '').split(',')[0].strip()) >= 3}
_ROSTER_NAME_RX = re.compile(r'\b(' + '|'.join(re.escape(x) for x in sorted(_SURNAMES)) + r')\b')


def run_fixtures(b, detections_path):
    jsonl_dir = Path(detections_path).parent
    eager = 'eager' in str(detections_path)
    """Per-rule regression fixtures (cards-process discipline #2). Returns
    {rule: True|False|'skip'|'manual'}. The suite only ever grows."""
    fx = {}
    blend = [x for x in b if x['src'] == 'blend']
    ts = [x['video_time_s'] for x in b]
    # R1: every high-conf priority event in the detections has a line within 8s
    dp = Path(detections_path)
    if dp.exists():
        dets = [json.loads(l) for l in open(dp) if l.strip()]
        misses, seen = [], set()
        KW = {'yellow_card': r'\byellow\b|\bcard(ed)?\b|\bbook(ed|ing|s)?\b|\bcaution(ed)?\b',
              'red_card': r'\bred card\b|\bsent off\b|\bdismissed\b|\bsecond yellow\b',
              'goal': GOAL_CALL_RX,
              'penalty': r'\bpenalt(y|ies)\b|\bspot[- ]kick\b'}
        goal_ts = [d2['t_det'] for d2 in dets
                   if any(e2.get('type') == 'goal' and e2.get('confidence') == 'high'
                          for e2 in (d2['det'].get('events') or []))]
        card_ts = {}
        for d2 in dets:
            for e2 in (d2['det'].get('events') or []):
                if e2.get('type') in ('yellow_card', 'red_card') and e2.get('confidence') == 'high':
                    card_ts.setdefault(e2['type'], []).append(d2['t_det'])
        for d in dets:
            for e in (d['det'].get('events') or []):
                et = e.get('type')
                if et == 'goal':
                    near = [g for g in goal_ts if abs(g - d['t_det']) <= 10]
                    if len(near) < 3 or (max(near) - min(near)) < 5.0:
                        continue    # R10-uncorroborated blip — correctly silenced, no line owed
                speak_t = d['t_det']
                if et in ('yellow_card', 'red_card'):
                    near = sorted(g for g in card_ts.get(et, []) if abs(g - d['t_det']) <= 10)
                    if len(near) < 2:
                        continue    # lone-blip card (e.g. 281.6s trio run2) — correctly silenced
                    speak_t = near[1]   # corroboration time: the event becomes speakable at the
                                        # 2nd sighting — the 8s clock starts THERE (trio r3: line
                                        # at 195.6 was 8.6s after 1st sighting but 3.1s after 2nd)
                if et in PRIORITY_EVENTS and e.get('confidence') == 'high':
                    bucket = (et, int(d['t_det'] // 30))
                    if bucket in seen:
                        continue
                    seen.add(bucket)
                    # the SPOKEN TEXT must reference the event (word-boundary patterns);
                    # hidden vision metadata no longer satisfies R1 (codex-4 #2)
                    hit = any(-2 <= x['video_time_s'] - speak_t <= 8 and
                              re.search(KW[et], x['text'], re.I)
                              for x in b)
                    if not hit:
                        misses.append((round(d['t_det'], 1), et))
        fx['R1'] = misses if misses else True
        if misses:
            fx['R1'] = False
    else:
        fx['R1'] = 'skip'
    # R2: filler lines must be >=15s from the previous line
    r2 = True
    for i, x in enumerate(b):
        if x['src'] == 'blend' and FILLER_RX.search(x['text']):
            if i > 0 and x['video_time_s'] - b[i - 1]['video_time_s'] < 15:
                r2 = False
    fx['R2'] = r2
    # R3: same event-fact twice within 25s requires a roster name in the second
    r3 = True
    ev_lines = [(x['video_time_s'], str(x.get('vision') or ''), x['text'])
                for x in blend if 'event:' in str(x.get('vision') or '')]
    for i in range(1, len(ev_lines)):
        t2, f2, tx2 = ev_lines[i]
        for t1, f1, _ in ev_lines[:i]:
            if f1.split('—')[0] == f2.split('—')[0] and t2 - t1 < 25:
                if not (_ROSTER_NAME_RX.search(tx2)
                        or re.search(r'\b(another|second|a further|one more)\b', tx2, re.I)):
                    r3 = False
    fx['R3'] = r3
    # R4: adjacent possession flips need a transition marker
    r4 = True
    for i in range(1, len(b)):
        if b[i]['src'] != 'blend' or b[i]['video_time_s'] - b[i-1]['video_time_s'] > 12:
            continue
        m1, m2 = POSS_RX.search(b[i-1]['text']), POSS_RX.search(b[i]['text'])
        t1_, t2_ = _poss_team(m1), _poss_team(m2)
        if t1_ and t2_ and t1_ != t2_ and not TRANSITION_RX.search(b[i]['text']):
            r4 = False
    fx['R4'] = r4
    # R7: banned French calques never appear
    bad = [w for x in b for w in FR_BANNED if w in (x.get('fr') or '').lower()]
    fx['R7'] = False if bad else True
    # R1b: at most one card line per 30s (team labels flap; dedup is type-only)
    cards = [x['video_time_s'] for x in b
             if re.search(r'yellow|red card|booked|book\b', x['text'], re.I) and x['src'] == 'blend']
    fx['R1b'] = all(b2 - a2 > 30 for a2, b2 in zip(cards, cards[1:])) if len(cards) > 1 else True
    # R8: no spoken STT line may carry a cached-insane verdict
    sp = Path(str(jsonl_dir)) / ('stt_sanity_eager.json' if eager else 'stt_sanity.json')
    if sp.exists():
        sane = json.loads(sp.read_text())
        bad = [x['text'] for x in b if x['src'] == 'soniox' and sane.get(x['text']) is False]
        fx['R8'] = False if bad else True
    # R10: any goal-call line must be backed by >=3 high-conf goal detections spanning >=5s
    if dp.exists():
        goal_lines = [x for x in b
                      if GOAL_CALL_RX.search(x['text']) and x['src'] == 'blend']
        r10 = True
        for x in goal_lines:
            gs = [d['t_det'] for d in dets
                  if abs(d['t_det'] - x['video_time_s']) <= 10
                  and any(e.get('type') == 'goal' and e.get('confidence') == 'high'
                          for e in (d['det'].get('events') or []))]
            if len(gs) < 3 or (max(gs) - min(gs) if gs else 0) < 5.0:
                r10 = False
        fx['R10'] = r10
    else:
        fx['R10'] = 'skip'
    # R11: team-reference variety — no 3 consecutive team-referring blend lines
    # using the identical form for the same team
    TEAM_FORMS = {'Mainz': ['FSV Mainz', 'the home side', 'the hosts', 'the reds', 'Mainz'],
                  'Union': ['Union Berlin', 'the away side', 'the visitors', 'the men in green', 'Union']}
    r11 = True
    runs_form = []
    for x in b:
        if x['src'] != 'blend':
            runs_form.append(None); continue
        found = None
        for team, forms in TEAM_FORMS.items():
            for fm in forms:
                m = re.search(r'\b' + re.escape(fm) + r'\b', x['text'], re.I)
                if m and (found is None or m.start() < found[2]):
                    found = (team, fm.lower(), m.start())
        runs_form.append(found[:2] if found else None)
    streak = 1
    for i in range(1, len(runs_form)):
        if runs_form[i] and runs_form[i] == runs_form[i-1]:
            streak += 1
            if streak >= 3:
                r11 = False
        else:
            streak = 1
    fx['R11'] = r11
    # R13: never describe the picture (camera-ban) — any language. Regex so French
    # "dans le cadre DE cette rencontre" (= 'in the context of') is NOT a false positive.
    CAMERA_RX = re.compile(
        r"\bin (the )?(frame|shot|picture)\b"        # EN framing (in shot / in the frame ...)
        r"|\bon (the )?screen\b"
        r"|dans le cadre(?!\s+(de|du|des|d['’]))"  # FR camera framing, not "dans le cadre de ..."
        r"|à l['’]écran"                          # FR on-screen
        r"|na (tela|imagem)|no quadro",                # PT on-screen / in the image
        re.I)
    fx['R13'] = not any(CAMERA_RX.search(x.get(k) or '') for x in b for k in ('text', 'fr', 'pt'))
    # R12: DIRECT mis-attribution only — a card/goal/substitution that credits a team via
    # "for/pour <team>" whose team differs from the named player's roster team. Deliberately
    # NARROW (cards/goals/subs + explicit "for", not "against") to avoid false rejects on
    # legitimate possession lines. Resolved off the match's pre-match lineup (generic).
    try:
        _rp = os.environ.get('CLIP_ROSTER',
                             '/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json')
        _sr = json.load(open(_rp))
        sur2team = {}
        for _c in _sr['lineups']['lineups']['competitors']:
            _tm = 'Mainz' if 'Mainz' in _c.get('name', '') else 'Union'
            for _p in _c.get('players', []):
                _nm = _p.get('name', ''); _sur = _nm.split(',')[0].strip() if ',' in _nm else _nm
                if len(_sur) >= 3:                    # skip too-short surnames (collision risk)
                    sur2team[_sur] = _tm
    except Exception:
        sur2team = {}
    if sur2team:
        AWARD = (r'(?:free\s*kick|corner|throw[- ]?in|throw|foul|penalty|goal\s*kick|'
                 r'set[- ]?piece|spot[- ]?kick)')
        r12 = True
        for x in b:
            if x['src'] != 'blend':
                continue
            named = [s for s in sur2team if re.search(r'\b' + re.escape(s) + r'\b', x['text'])]
            if not named or len({sur2team[s] for s in named}) != 1:
                continue                             # no player, or both teams named (ambiguous)
            pteam = sur2team[named[0]]
            CGS = re.compile(r'yellow|red card|\bbooked\b|\bbook\b|sent off|dismissed|'
                             r'\bgoal\b(?!\s*kick)|scored|substitut|\bsub(bed)?\b', re.I)
            for team, forms in TEAM_FORMS.items():   # opposing-team reference that isn't an award
                if team == pteam:
                    continue
                for fm in forms:
                    # (a) explicit 'for/pour <team>' credit on ANY line
                    for m in re.finditer(r'\b(?:for|pour)\s+' + re.escape(fm) + r'\b', x['text'], re.I):
                        if not re.search(AWARD + r'\W*$', x['text'][:m.start()], re.I):
                            r12 = False
                    # (b) tight broadened (codex-3): opposing team DIRECTLY attached to a
                    # card verb — '<team> booked' / '<team> is|are booked'
                    if re.search(r'\b' + re.escape(fm) + r'\s+(?:is\s+|are\s+)?booked\b',
                                 x['text'], re.I):
                        r12 = False
        fx['R12'] = r12
    else:
        fx['R12'] = 'skip'
    # R14: event language requires an event fact IN THE LINE'S OWN grounding context
    # (deterministic hallucination gate — replaces the single-frame judge as the gate;
    # judge remains a watched tripwire). STT lines are verbatim broadcaster truth.
    EVLANG = {
        'save': r'\bsave[sd]?\b|\bsmothers\b|\bparr(y|ies|ied)\b',
        'free_kick': r'\bfree[- ]kick\b', 'corner': r'\bcorner\b',
        'penalty': r'\bpenalt',
        'goal': r'\bscor(es|ed)\b|\bgoal!|\bwhat a goal\b|\bfinds the net\b',
        'substitution': r'\bsubstitut|\bfresh legs\b|\bcomes? on\b|\bbrought on\b',
        'yellow_card': r'\byellow\b|\bbook(ed|ing|s)?\b|\bcaution',
        'red_card': r'\bred card\b|\bsent off\b|\bdismissed\b',
        'throw_in': r'\bthrow[- ]in\b', 'goal_kick': r'\bgoal[- ]kick\b',
        'shot': r'\bshot\b|\bshoots\b|\bfires\b|\bheader\b',
        'foul': r'\bfoul(ed)?\b',
    }
    r14 = True
    for x in b:
        if x['src'] != 'blend' or x.get('real_phrase'):
            continue
        ctx = (str(x.get('vision') or '') + ' ' + str(x.get('tracker') or '')).lower()
        for ev, rx in EVLANG.items():
            if re.search(rx, x['text'], re.I):
                if ev == 'shot' and ('shot' in ctx or 'goal' in ctx):
                    continue
                if ev not in ctx:
                    r14 = False
    fx['R14'] = r14
    # R15: spatial claim must agree with the recorded tracker third for the subject team
    OWN_RX = re.compile(r"(?:their|its)\s+own\s+(?:third|half)|\bown\s+third\b", re.I)
    FINAL_RX = re.compile(r"\bthe\s+final\s+third\b|\battacking\s+third\b", re.I)
    HOME_TEAM, AWAY_TEAM = 'Mainz', 'Union'   # from pre-match data (generic home/away frame)
    r15 = True
    for x in b:
        if x['src'] != 'blend' or x.get('real_phrase'):
            continue
        team = x.get('poss_team_ctx'); third = x.get('trk_third')
        if third not in ('home_defensive', 'home_attacking') or team not in (HOME_TEAM, AWAY_TEAM):
            continue
        zone = 'final' if ((team == HOME_TEAM) == (third == 'home_attacking')) else 'own'
        if zone == 'final' and OWN_RX.search(x['text']) and not FINAL_RX.search(x['text']):
            r15 = False
        if zone == 'own' and FINAL_RX.search(x['text']) and not OWN_RX.search(x['text']):
            r15 = False
    fx['R15'] = r15
    # R5/R6/R8: reviewer/judge-checked (no deterministic oracle)
    fx['R5'] = fx['R6'] = fx['R8'] = 'manual'
    return fx


GUARDED = [   # (key, predicate on (baseline, candidate), description)
    # hallucination gating is DETERMINISTIC via fixture R14 (event language requires an
    # event fact). The single-frame LLM judge is a watched tripwire only — 6/7 of its
    # flags in the 2026-07-24 trio were provably grounded lines (see ledger amendment).
    ('survival', lambda b, c: c is not None and c >= 0.95, 'survival >= 0.95 HARD (never relaxed by a deficient baseline); missing = FAIL'),
    ('desync_shifts_gt_1_5', lambda b, c: c == 0, 'no desync shifts'),
    ('first_line_s', lambda b, c: c is not None and c <= 2.0, 'first line within 2s'),
]
WATCHED = ['hallucinations', 'judge_failures', 'words', 'gaps_ge_15s', 'max_gap_s', 'named_blend_lines', 'fr_track_missing', 'pt_track_missing',
           'judge_realism', 'judge_variety', 'stt_lines', 'lines']


def _worst_or_fail(v, agg, fail):
    # fail-closed: a run with a MISSING guarded metric counts as the failing value
    return agg([fail if x is None else x for x in v])
WORST = {'survival': lambda v: _worst_or_fail(v, min, 0.0),
         'desync_shifts_gt_1_5': lambda v: _worst_or_fail(v, max, 999),
         'first_line_s': lambda v: _worst_or_fail(v, max, 999.0)}


def compare(base, cands):
    """Gate baseline vs one or more candidate runs. Multiple candidates = the
    stochasticity discipline: guarded metrics judged on the WORST run; fixtures
    must pass in ALL runs. Thresholds may only change via a logged amendment in
    tuning_rules.yaml (no-silent-relaxation)."""
    if isinstance(cands, dict):
        cands = [cands]
    verdict = 'ACCEPT'
    print(f"{'metric':24s} {'baseline':>10s} {'worst-of-' + str(len(cands)):>12s}  gate")
    for k, pred, desc in GUARDED:
        bv = base.get(k)
        vals = [c.get(k) for c in cands]
        cv = WORST[k](vals) if k in WORST else vals[0]
        ok = pred(bv, cv)
        spread = '' if len(cands) == 1 else f"  (runs: {vals})"
        print(f"{k:24s} {str(bv):>10s} {str(cv):>12s}  {'PASS' if ok else 'FAIL — ' + desc}{spread}")
        if not ok:
            verdict = 'REJECT'
    print('--- per-rule fixtures (auto: must be True in ALL runs; manual: reviewer-checked) ---')
    AUTO = {'R1','R1b','R2','R3','R4','R7','R10','R11','R12','R13','R14','R15'}
    rules = sorted({r for c in cands for r in c.get('fixtures', {})} | AUTO)
    for r in rules:
        vals = [c.get('fixtures', {}).get(r) for c in cands]
        if r in AUTO:
            ok = all(v is True for v in vals)          # fail-closed: missing/skip -> FAIL
            if not ok:
                verdict = 'REJECT'
            print(f"{r:24s} {('PASS' if ok else 'FAIL'):>10s}  {vals}")
        else:
            print(f"{r:24s} {str(vals[0]):>10s}   (manual)")
    print('--- watched (no gate, report only) ---')
    for k in WATCHED:
        vals = [c.get(k) for c in cands]
        print(f"{k:24s} {str(base.get(k)):>10s} {str(vals):>18s}")
    print(f"\nVERDICT: {verdict}")
    return verdict


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'snapshot'
    if cmd == 'snapshot':
        path = sys.argv[2] if len(sys.argv) > 2 else BASE / 'commentary_blend_live_eager.jsonl'
        skip = '--fast' in sys.argv
        print(json.dumps(snapshot(path, skip_llm=skip), indent=2))
    elif cmd == 'compare':
        base = json.loads(Path(sys.argv[2]).read_text())
        cands = [json.loads(Path(a).read_text()) for a in sys.argv[3:]]
        v = compare(base, cands)
        sys.exit(0 if v == 'ACCEPT' else 1)

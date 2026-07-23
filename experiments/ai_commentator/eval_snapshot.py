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
import json, re, sys
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
        gen = [x for x in b if x['src'] == 'blend']
        with ThreadPoolExecutor(max_workers=6) as ex:
            vs = list(ex.map(lambda x: J.judge_line(x['text'], J.frame_for_time_s(x['video_time_s'])), gen))
        hall = sum(1 for v in vs if v and str(v.get('hallucination_likely')).lower() == 'true')
        style = J.judge_run_style([x['text'] for x in b])
        snap['hallucinations'] = hall
        snap['judge_realism'] = style.get('realism_1_5')
        snap['judge_variety'] = style.get('variety_1_5')
    return snap


PRIORITY_EVENTS = {'yellow_card', 'red_card', 'goal', 'penalty'}
FILLER_RX = re.compile(r'quiet spell|midfield battle continues|still all square', re.I)
FR_BANNED = ['sonder', 'dernier tiers', 'moment calme']
TRANSITION_RX = re.compile(r'win|won|regain|turn|steal|intercept|back|break|rob|force', re.I)
POSS_RX = re.compile(r'\b(Mainz|Union)\b.{0,30}\b(possess|keep|on the ball|have it|work|circulat|hold)', re.I)


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
        KW = {'yellow_card': ('yellow', 'card', 'book'), 'red_card': ('red', 'card'),
              'goal': ('goal',), 'penalty': ('penalty',)}
        goal_ts = [d2['t_det'] for d2 in dets
                   if any(e2.get('type') == 'goal' and e2.get('confidence') == 'high'
                          for e2 in (d2['det'].get('events') or []))]
        for d in dets:
            for e in (d['det'].get('events') or []):
                et = e.get('type')
                if et == 'goal':
                    near = [g for g in goal_ts if abs(g - d['t_det']) <= 10]
                    if len(near) < 3 or (max(near) - min(near)) < 5.0:
                        continue    # R10-uncorroborated blip — correctly silenced, no line owed
                if et in PRIORITY_EVENTS and e.get('confidence') == 'high':
                    bucket = (et, int(d['t_det'] // 30))
                    if bucket in seen:
                        continue
                    seen.add(bucket)
                    hit = any(abs(x['video_time_s'] - d['t_det']) <= 8 and
                              (any(k in x['text'].lower() for k in KW[et]) or
                               et in str(x.get('vision') or ''))
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
                if not (re.search(r'[A-Z][a-z]+', tx2.replace(tx2.split()[0], '', 1))
                        or re.search(r'\b(another|second|a further|one more)\b', tx2, re.I)):
                    r3 = False
    fx['R3'] = r3
    # R4: adjacent possession flips need a transition marker
    r4 = True
    for i in range(1, len(b)):
        if b[i]['src'] != 'blend' or b[i]['video_time_s'] - b[i-1]['video_time_s'] > 12:
            continue
        m1, m2 = POSS_RX.search(b[i-1]['text']), POSS_RX.search(b[i]['text'])
        if m1 and m2 and m1.group(1) != m2.group(1) and not TRANSITION_RX.search(b[i]['text']):
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
        goal_lines = [x for x in b if re.search(r'\bscored\b|\bgoal\b(?!\s*kick)', x['text'], re.I)
                      and x['src'] == 'blend']
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
    # R13: never describe the picture (camera-ban) — any language
    CAMERA_BAN = ['in the frame', 'in frame', 'in shot', 'in the picture', 'on screen',
                  'on the screen', 'dans le cadre', 'na imagem', 'no quadro']
    fx['R13'] = not any(p in (x.get(k) or '').lower()
                        for x in b for k in ('text', 'fr', 'pt') for p in CAMERA_BAN)
    # R12: a team-specific event naming a roster player must attribute it to that
    # player's roster team (generic — resolved off the match's pre-match lineup).
    try:
        _sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
        sur2team = {}
        for _c in _sr['lineups']['lineups']['competitors']:
            _tm = 'Mainz' if 'Mainz' in _c.get('name', '') else 'Union'
            for _p in _c.get('players', []):
                _nm = _p.get('name', ''); _sur = _nm.split(',')[0].strip() if ',' in _nm else _nm
                if _sur:
                    sur2team[_sur] = _tm
    except Exception:
        sur2team = {}
    if sur2team:
        EVENT_RX = re.compile(r'yellow|red card|booked|book\b|\bgoal\b|scored|substitut|'
                              r'\bsub\b|free kick|corner|throw', re.I)
        r12 = True
        for x in b:
            if x['src'] != 'blend' or not EVENT_RX.search(x['text']):
                continue
            named = [s for s in sur2team if re.search(r'\b' + re.escape(s) + r'\b', x['text'])]
            if not named:
                continue
            stated = None
            for team, forms in TEAM_FORMS.items():
                if any(re.search(r'\b' + re.escape(fm) + r'\b', x['text'], re.I) for fm in forms):
                    stated = team; break
            if stated and any(sur2team[s] != stated for s in named):
                r12 = False
        fx['R12'] = r12
    else:
        fx['R12'] = 'skip'
    # R5/R6/R8: reviewer/judge-checked (no deterministic oracle)
    fx['R5'] = fx['R6'] = fx['R8'] = 'manual'
    return fx


GUARDED = [   # (key, predicate on (baseline, candidate), description)
    ('hallucinations', lambda b, c: c <= max(b, 0), 'hallucinations must stay at baseline (target 0)'),
    ('survival', lambda b, c: c is None or c >= min(b or 1.0, 0.95), 'survival >= 0.95 (or baseline if lower)'),
    ('desync_shifts_gt_1_5', lambda b, c: c == 0, 'no desync shifts'),
    ('first_line_s', lambda b, c: c is not None and c <= 2.0, 'first line within 2s'),
]
WATCHED = ['words', 'gaps_ge_15s', 'max_gap_s', 'named_blend_lines', 'fr_track_missing', 'pt_track_missing',
           'judge_realism', 'judge_variety', 'stt_lines', 'lines']


WORST = {'hallucinations': lambda v: max(v),
         'survival': lambda v: min(x for x in v if x is not None),
         'desync_shifts_gt_1_5': lambda v: max(v),
         'first_line_s': lambda v: max(x for x in v if x is not None)}


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
    AUTO = {'R1','R1b','R2','R3','R4','R7','R10','R11','R12','R13'}
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

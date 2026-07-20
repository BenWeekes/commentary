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
    rep_file = BASE / ('latency_report_eager.json' if 'eager' in str(jsonl_path)
                       else 'latency_report.json')
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
    }
    if not skip_llm:
        import judge as J
        gen = [x for x in b if x['src'] == 'blend']
        hall = 0
        for x in gen:
            v = J.judge_line(x['text'], J.frame_for_time_s(x['video_time_s']))
            if v and str(v.get('hallucination_likely')).lower() == 'true':
                hall += 1
        style = J.judge_run_style([x['text'] for x in b])
        snap['hallucinations'] = hall
        snap['judge_realism'] = style.get('realism_1_5')
        snap['judge_variety'] = style.get('variety_1_5')
    return snap


GUARDED = [   # (key, predicate on (baseline, candidate), description)
    ('hallucinations', lambda b, c: c <= max(b, 0), 'hallucinations must stay at baseline (target 0)'),
    ('survival', lambda b, c: c is None or c >= min(b or 1.0, 0.95), 'survival >= 0.95 (or baseline if lower)'),
    ('desync_shifts_gt_1_5', lambda b, c: c == 0, 'no desync shifts'),
    ('first_line_s', lambda b, c: c is not None and c <= 2.0, 'first line within 2s'),
]
WATCHED = ['words', 'gaps_ge_15s', 'max_gap_s', 'named_blend_lines',
           'judge_realism', 'judge_variety', 'stt_lines', 'lines']


def compare(base, cand):
    verdict = 'ACCEPT'
    print(f"{'metric':24s} {'baseline':>10s} {'candidate':>10s}  gate")
    for k, pred, desc in GUARDED:
        bv, cv = base.get(k), cand.get(k)
        ok = pred(bv, cv)
        print(f"{k:24s} {str(bv):>10s} {str(cv):>10s}  {'PASS' if ok else 'FAIL — ' + desc}")
        if not ok:
            verdict = 'REJECT'
    print('--- watched (no gate, report only) ---')
    for k in WATCHED:
        print(f"{k:24s} {str(base.get(k)):>10s} {str(cand.get(k)):>10s}")
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
        cand = json.loads(Path(sys.argv[3]).read_text())
        v = compare(base, cand)
        sys.exit(0 if v == 'ACCEPT' else 1)

#!/usr/bin/env python3
"""Render the tracker column for the page: objective location/shape read from
the YOLO+homography tracker (events_tracker.jsonl) as short readable lines.

Tracker gives objective truth (ball third via homography, team head-counts,
possession side) — NOT events. We render possession team + third + side, and a
"numbers back" note when one side is clearly stacked, collapsing repeats.

Output -> tracker_col.jsonl  {video_time_s, text, detail}
"""
import json
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
SRC = BASE / 'events_tracker.jsonl'
OUT = BASE / 'tracker_col.jsonl'
TEAM = {'home': 'Mainz', 'away': 'Union'}
THIRD = {'home_defensive': 'back third', 'middle': 'midfield', 'home_attacking': 'final third'}


def render(det):
    if not det:
        return None
    tr = det.get('tracker') or {}
    p = det.get('possession') or {}
    third = THIRD.get(tr.get('ball_third') or p.get('third'), '')
    team = TEAM.get(p.get('team'))
    main = None
    if team and third:
        main = f"{team} in possession — {third}"
    elif team:
        main = f"{team} in possession"
    elif third:
        main = f"ball in the {third}"
    if not main:
        return None
    bits = []
    m, u = tr.get('mainz', 0), tr.get('union', 0)
    if m + u >= 7 and abs(m - u) >= 3:
        bits.append((f"{'Mainz' if m > u else 'Union'} have numbers back"))
    if p.get('side') in ('left', 'centre', 'right'):
        bits.append(p['side'])
    n = tr.get('players')
    if n:
        bits.append(f"{n} tracked")
    return main, ' · '.join(bits)


def main():
    out = []
    for line in open(SRC):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        det, t = r.get('detection'), r.get('video_time_s')
        if t is None or not det:
            continue
        res = render(det)
        if not res:
            continue
        text, detail = res
        if out and out[-1]['text'] == text and float(t) - out[-1]['_last'] <= 6.0:
            out[-1]['_last'] = round(float(t), 2)
            if detail:
                out[-1]['detail'] = detail
            continue
        out.append({'video_time_s': round(float(t), 2), 'text': text, 'detail': detail,
                    '_last': round(float(t), 2)})
    for o in out:
        o.pop('_last', None)
    OUT.write_text('\n'.join(json.dumps(o, ensure_ascii=False) for o in out) + '\n')
    print(f"{len(out)} tracker lines -> {OUT}")
    for o in out[:10]:
        print(f"  {o['video_time_s']:6.1f}s {o['text']}  ({o['detail']})")


if __name__ == '__main__':
    main()

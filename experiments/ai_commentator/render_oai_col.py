#!/usr/bin/env python3
"""Render the OpenAI gpt-5.5 (4-frame) detector column: turn each structured
detection into ONE short readable line, GATED by the detector's own confidence.

- events: keep when event.confidence in {high, medium}; drop 'low' and replays.
- possession: keep when possession.team is a real side AND confidence in
  {high, medium}; name the carrier only when the shirt number validates against
  the roster for that team (else team + pitch-third). Low confidence -> hole.

Output -> oai_col.jsonl  {video_time_s, text, conf, kind}
Usage: python render_oai_col.py [events_gpt55.jsonl]
"""
import json, sys
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
sys.path.insert(0, str(BASE))
from run_v5 import build_match_context

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / 'events_gpt55.jsonl'
OUT = BASE / 'oai_col.jsonl'
TEAM = {'home': 'Mainz', 'away': 'Union'}
ROSTER_TEAM = {'home': 'FSV Mainz', 'away': 'Union Berlin'}
THIRD = {'home_defensive': 'back third', 'middle': 'midfield', 'home_attacking': 'final third'}
KEEP_CONF = {'high', 'medium'}
EVENT_VERB = {
    'foul': 'Foul', 'shot': 'Shot', 'goal': 'GOAL', 'save': 'Save',
    'corner': 'Corner', 'free_kick': 'Free kick', 'throw_in': 'Throw-in',
    'offside': 'Offside', 'card': 'Card', 'substitution': 'Substitution',
    'tackle': 'Tackle', 'header': 'Header', 'cross': 'Cross', 'goal_kick': 'Goal kick',
}


def name_for(num, team, name_by):
    """Name the carrier only if the shirt number belongs to that team's roster."""
    if num is None:
        return None
    p = name_by.get(str(num))
    if p and p['team'] == ROSTER_TEAM.get(team):
        return p['short_name']
    return None


def render(det, name_by):
    if not det:
        return None
    # (1) prefer a confident, real event — headline + the model's visual evidence
    for e in (det.get('events') or []):
        et = e.get('type')
        if not et or et in ('replay_starts', 'replay_ends') or e.get('confidence') not in KEEP_CONF:
            continue
        verb = EVENT_VERB.get(et, et.replace('_', ' ').capitalize())
        tm = TEAM.get(e.get('team'))
        oc = e.get('shot_outcome')
        s = verb + (f" — {tm}" if tm else '')
        if oc:
            s += f" ({oc})"
        detail = (e.get('visual_evidence') or '').strip()
        if len(detail) > 120:
            detail = detail[:117].rsplit(' ', 1)[0] + '…'
        return s, detail, e.get('confidence'), 'event'
    # (2) else confident possession — headline + third/side/pressure
    p = det.get('possession') or {}
    team = p.get('team')
    if team in ('home', 'away') and p.get('confidence') in KEEP_CONF:
        tm = TEAM[team]
        nm = name_for(p.get('player_shirt_number'), team, name_by)
        third = THIRD.get(p.get('third'), '')
        who = nm if nm else tm
        s = (f"{who} on the ball" if nm else f"{tm} in possession")
        bits = [b for b in (third,
                            (p.get('side') if p.get('side') in ('left', 'centre', 'right') else ''),
                            ('under pressure' if p.get('under_pressure') is True else '')) if b]
        return s, ' · '.join(bits), p.get('confidence'), 'possession'
    return None


def main():
    ctx = build_match_context()
    name_by = {str(pp['number']): {'short_name': pp['short_name'], 'team': pp['team']}
               for pp in ctx['roster']}
    recs = [json.loads(l) for l in open(SRC) if l.strip()]
    out = []
    for r in recs:
        det = r.get('detection')
        t = r.get('video_time_s')
        if t is None or not det:
            continue
        res = render(det, name_by)
        if not res:
            continue
        text, detail, conf, kind = res
        # collapse consecutive runs of the SAME line (bursts fire ~1/s) -> one row
        # at the run's start; keep the strongest confidence seen in the run.
        rank = {'low': 0, 'medium': 1, 'high': 2}
        if out and out[-1]['text'] == text and float(t) - out[-1]['_last'] <= 6.0:
            out[-1]['_last'] = round(float(t), 2)
            if rank.get(conf, 0) > rank.get(out[-1]['conf'], 0):
                out[-1]['conf'] = conf
                if detail:
                    out[-1]['detail'] = detail
            continue
        out.append({'video_time_s': round(float(t), 2), 'text': text, 'detail': detail,
                    'conf': conf, 'kind': kind, '_last': round(float(t), 2)})
    for o in out:
        o.pop('_last', None)
    OUT.write_text('\n'.join(json.dumps(o, ensure_ascii=False) for o in out) + '\n')
    ne = sum(1 for o in out if o['kind'] == 'event')
    print(f"{len(recs)} detections -> {len(out)} confident lines ({ne} events, {len(out)-ne} possession); "
          f"{len(recs)-len(out)} holes (low-confidence / empty)")
    for o in out[:16]:
        print(f"  {o['video_time_s']:6.1f}s [{o['conf']:>6}] {o['text']}")


if __name__ == '__main__':
    main()

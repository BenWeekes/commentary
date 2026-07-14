#!/usr/bin/env python3
"""Fact + phrase composer — the payoff of the vision/STT eval.

Builds ONE commentary track by:
  1. Using short standalone Soniox utterances VERBATIM where they exist (real
     broadcaster language, zero hallucination) — the anchors.
  2. Filling the gaps with GROUNDED generated lines from the detector facts —
     possession ("Amiri takes it down the right"), named via roster from the
     shirt number, located via the tracker's homography third, plus events.
     A player name is a winner; team-level when the number isn't known.

Nothing is invented: generated lines are constrained to detector facts, and
we only speak possession/events the detector reported. Soniox always wins.

Output: composed_commentary.jsonl + printed interleaved transcript.
Usage: python compose_commentary.py
"""
import json, os, re, sys, time
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('='); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
sys.path.insert(0, str(BASE))
from run_v5 import build_match_context

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
GEN_MODEL = 'gpt-5.4-mini'
TEAM = {'home': 'Mainz', 'away': 'Union'}
THIRD = {'home_defensive': 'in their own third', 'middle': 'in midfield', 'home_attacking': 'in the final third'}


def load(f):
    p = BASE / f
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


def by_time(recs):
    return {round(float(r['video_time_s']), 1): r['detection'] for r in recs if 'detection' in r}


def near(m, t, w=1.6):
    ks = [k for k in m if abs(k - t) <= w]
    return m[min(ks, key=lambda k: abs(k - t))] if ks else None


GEN_SYSTEM = """You are a live football commentator. Write ONE short spoken line
(4-9 words) for THIS moment, using ONLY the grounded facts given. Rules:
- Name the player if one is given; otherwise use the team or a generic role.
- Do NOT invent events, shots, goals, or anything not in the facts.
- Vary phrasing/verbs from the recent lines. No scoreline.
Return only the line."""


def gen_line(fact, recent):
    rc = "\n".join(f"  - {r}" for r in recent[-6:]) or "  - none"
    user = f"FACTS: {fact}\nRECENT LINES (avoid repeating):\n{rc}\nLine:"
    try:
        r = client.responses.create(model=GEN_MODEL, instructions=GEN_SYSTEM,
                                    input=[{"role": "user", "content": user}], max_output_tokens=40)
        return re.sub(r'\s+', ' ', (r.output_text or '').strip().strip('"'))
    except Exception as e:
        return None


def fact_for(det, trk_det, name_by_num):
    """Build a grounded fact string from the OpenAI detector (+tracker area)."""
    if not det:
        return None
    evs = det.get('events') or []
    p = det.get('possession') or {}
    team = TEAM.get(p.get('team'))
    num = p.get('player_shirt_number')
    name = name_by_num.get(str(num)) if num is not None else None
    area = ''
    if trk_det and (trk_det.get('tracker') or {}).get('ball_third'):
        area = ', ' + THIRD.get(trk_det['tracker']['ball_third'], '')
    # prefer an event (more interesting), else possession
    for e in evs:
        et = e.get('type'); etm = TEAM.get(e.get('team'), '')
        if et and et not in ('replay_starts',):
            return f"event: {et}" + (f" for {etm}" if etm else '') + area
    if team:
        who = f"player: {name}" if name else "player: unknown"
        return f"possession: {team}, {who}{area}"
    return None


def main():
    ctx = build_match_context()
    name_by_num = {str(pp['number']): pp['short_name'] for pp in ctx['roster']}
    short = sorted(load('soniox_short.jsonl'), key=lambda r: r['video_time_s'])
    oai = by_time(load('events_gpt55.jsonl'))
    trk = by_time(load('events_tracker.jsonl'))

    # candidate slots: soniox (verbatim) + a generation slot at each detector burst
    cands = []
    for u in short:
        cands.append((u['video_time_s'], 'soniox', u))
    for t in sorted(oai):
        cands.append((t, 'gen', t))
    cands.sort(key=lambda c: c[0])
    son_starts = [u['video_time_s'] for u in short]

    out = []
    booth = 0.0
    for t, kind, payload in cands:
        if t < booth:
            continue
        if kind == 'soniox':
            u = payload
            out.append({'video_time_s': round(t, 2), 'src': 'soniox', 'text': u['text'],
                        'conf': u.get('conf'), 'dur': u.get('dur')})
            booth = t + (u.get('dur') or 2.0) + 0.3
        else:
            # yield to an imminent real phrase
            if any(0 <= s - t <= 2.0 for s in son_starts):
                continue
            fact = fact_for(near(oai, t), near(trk, t), name_by_num)
            if not fact:
                continue
            recent = [o['text'] for o in out[-6:]]
            line = gen_line(fact, recent)
            if not line or line.upper().startswith('NO'):
                continue
            est = max(1.5, len(line.split()) / 2.6)
            out.append({'video_time_s': round(t, 2), 'src': 'gen', 'text': line, 'fact': fact})
            booth = t + est + 0.4
        if len(out) % 15 == 0:
            print(f"  ...{len(out)} lines, t={t:.0f}s")

    (BASE / 'composed_commentary.jsonl').write_text(
        '\n'.join(json.dumps(o, ensure_ascii=False) for o in out) + '\n')
    ns = sum(1 for o in out if o['src'] == 'soniox'); ng = len(out) - ns
    print(f"\n=== COMPOSED TRACK: {len(out)} lines ({ns} Soniox verbatim, {ng} generated) ===\n")
    for o in out:
        tag = 'SONIOX' if o['src'] == 'soniox' else '  gen '
        extra = f"   [{o['fact']}]" if o['src'] == 'gen' else f"   (c{o['conf']})"
        print(f"  {o['video_time_s']:6.1f}s [{tag}] {o['text']}{extra}")


if __name__ == '__main__':
    main()

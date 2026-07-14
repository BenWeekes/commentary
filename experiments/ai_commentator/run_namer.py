#!/usr/bin/env python3
"""Naming-maximiser: for each sampled burst, get gpt-5.5 to name — with a
confidence — who HAS the ball, who it is PASSED TO, and any other clearly
identifiable players, from shirt numbers + kit + position + roster.

Output is structured so a HUMAN can verify naming accuracy against the frame
(build_namer_review.py renders the review page). This replaces single-frame
"hallucination" scoring with the metric that matters: player-name accuracy.

Usage:
  python run_namer.py [stride] [limit]
    stride : sample every Nth master frame (default 5 ≈ 2.75s)
    limit  : cap number of bursts (for a quick test); 0 = all
"""
from __future__ import annotations
import base64, json, os, re, sys, time, concurrent.futures
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v5 import build_match_context
from rich_context import build_rich_context_text

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
SAMPLE_INTERVAL_S = 0.55
BURST = 4
MODEL = 'gpt-5.5'

NAMER_SYSTEM = """You identify football players in a short broadcast frame burst
(oldest to newest, ~0.55s apart). This is Mainz 05 (red/white) vs Union Berlin.

Identify players you are CONFIDENT about, using: visible shirt number on the
back, kit colour, goalkeeper position, and the ROSTER provided. Name as many as
you can that you are confident about — but ONLY when confident.

Return STRICT JSON:
{
  "has_ball":  {"name": "<surname or null>", "shirt": "<number or null>", "conf": "high|medium|low"},
  "pass_to":   {"name": "<surname or null>", "conf": "high|medium|low"},   // player the ball is travelling TO across the burst, if a pass is visible; else null
  "others_named": ["<surname>", ...],   // other players you can confidently name in the newest frame
  "line": "<one natural commentary line that names the player(s), e.g. 'Amiri slides it to Caci on the left'>"
}

Rules:
- Put a NAME only when genuinely confident (clear number, or unmistakable
  keeper/position). Otherwise use null and conf "low". Do not guess a specific
  name just to fill the field.
- Prefer naming when confident. It is fine for has_ball to be null if truly unclear.
- Goalkeepers: Klaus (Union), Zentner (Mainz).
Return ONLY the JSON."""


def encode_jpeg(p):
    return base64.b64encode(Path(p).read_bytes()).decode()


def name_burst(client, burst_paths, roster_text):
    content = [{"type": "input_text", "text": f"ROSTER:\n{roster_text}\n\nName players per schema."}]
    for p in burst_paths:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(model=MODEL, instructions=NAMER_SYSTEM,
            input=[{"role": "user", "content": content}],
            max_output_tokens=600, reasoning={"effort": "low"})
        raw = (resp.output_text or '').strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
    except Exception as e:
        obj = {'error': str(e)[:150]}
    obj['_ms'] = int((time.monotonic() - t0) * 1000)
    return obj


def main():
    stride = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    frames = sorted(FRAMES_DIR.glob('f_*.jpg'))
    ctx = build_match_context()
    roster_text = "\n".join(
        f"#{p.get('number','?')} {p['short_name']} ({p.get('position','')}, {p.get('team','')})"
        for p in ctx['roster']) if ctx.get('roster') else build_rich_context_text(ctx)[:2000]
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    idxs = list(range(BURST - 1, len(frames), stride))
    if limit: idxs = idxs[:limit]
    print(f"naming {len(idxs)} bursts (stride={stride}) over {len(frames)} frames")

    def do(i):
        burst = frames[i - BURST + 1: i + 1]
        r = name_burst(client, burst, roster_text)
        return {'video_time_s': round((i + 1) * SAMPLE_INTERVAL_S, 2),
                'newest_frame': frames[i].name, **r}

    out = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for k, rec in enumerate(ex.map(do, idxs)):
            out.append(rec)
            hb = (rec.get('has_ball') or {}).get('name')
            pt = (rec.get('pass_to') or {}).get('name')
            if k % 10 == 0 or (hb or pt):
                print(f"  [{k+1}/{len(idxs)}] t={rec['video_time_s']:6.1f}s has={hb} to={pt} "
                      f"others={rec.get('others_named')}  {(rec.get('line') or '')[:50]!r}")
    with open(BASE / 'namer.jsonl', 'w') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    named = sum(1 for r in out if (r.get('has_ball') or {}).get('name'))
    passes = sum(1 for r in out if (r.get('pass_to') or {}).get('name'))
    print(f"\nwrote namer.jsonl — {len(out)} bursts, {named} with a named ball-carrier, "
          f"{passes} with a named pass target. wall={time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()

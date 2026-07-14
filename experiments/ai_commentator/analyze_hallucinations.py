#!/usr/bin/env python3
"""Is the tracker accurate AT the frames where the commentary hallucinates?

For every hallucinated line in a judge_<variant>.json, we pull the Tier-A
tracker output for that frame and ask gpt-5.5 (with the actual frame) two things:
  1. tracking_accurate  — do the tracker's stated facts (view type, ball zone,
                          team presence) actually match this frame?
  2. contradicts_line   — do those tracker facts contradict / make implausible
                          the hallucinated commentary line (i.e. could grounding
                          on the tracker have PREVENTED this hallucination)?

This separates two failure modes:
  - tracker accurate + contradicts line   -> grounding SHOULD fix it (prompt/OCR work)
  - tracker accurate + does NOT contradict-> hallucination is beyond Tier A (event/identity)
  - tracker INaccurate                     -> tracker itself failed here (needs Tier B/C)

Usage:
  python analyze_hallucinations.py judge_<variant>.json
"""
import json, sys, os, re, base64
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
sys.path.insert(0, str(BASE))
from run_gpt55_track import load_tracking, format_tracking
from judge import frame_for_time_s, encode_jpeg

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
MODEL = 'gpt-5.5'

AUDIT_SYSTEM = """You audit an external object-tracker against a football frame.
You are given: (a) the actual video frame, (b) the tracker's stated facts for it,
(c) a commentary line the AI produced that was flagged as a likely HALLUCINATION.

Return STRICT JSON:
{
  "tracking_accurate": 0 or 1,   // 1 if the tracker's stated view-type (live vs replay/close-up), ball location, and team presence genuinely match what's in the frame
  "contradicts_line": 0 or 1,    // 1 if the tracker's facts contradict or make implausible the commentary line (i.e. trusting the tracker would have prevented this line)
  "tracker_error": "",           // if tracking_accurate=0, one phrase on what the tracker got wrong; else ""
  "rationale": "one sentence"
}
Return ONLY JSON."""


def audit(line_text, frame_path, tracking_block):
    content = [{"type": "input_text",
                "text": f"COMMENTARY LINE (flagged hallucination): \"{line_text}\"\n\n"
                        f"{tracking_block or 'TRACKER: (no data for this frame)'}\n\n"
                        f"Frame attached. Audit per schema."}]
    if frame_path:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(frame_path)}"})
    try:
        resp = client.responses.create(model=MODEL, instructions=AUDIT_SYSTEM,
            input=[{"role": "user", "content": content}],
            max_output_tokens=500, reasoning={"effort": "low"})
        m = re.search(r'\{.*\}', (resp.output_text or '').strip(), re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"  audit error: {e}", file=sys.stderr); return None


def main():
    judge_path = Path(sys.argv[1])
    variant = judge_path.stem.replace('judge_', '')
    data = json.load(open(judge_path))
    verdicts = data.get('verdicts', [])
    hallus = [v for v in verdicts if v.get('hallucination_likely') == 1]
    print(f"{variant}: {len(hallus)} hallucinated lines of {len(verdicts)} judged\n")

    track_by_frame, teams = load_tracking()
    results = []
    for v in hallus:
        t = v.get('_t', 0); text = v.get('_text', '')
        frame = frame_for_time_s(t)
        rec = track_by_frame.get(frame.name) if frame else None
        block = format_tracking(rec, teams) if rec else None
        a = audit(text, frame, block)
        if not a:
            continue
        a['_t'] = t; a['_text'] = text
        a['_view'] = (block.split('\n')[1] if block else 'n/a')
        results.append(a)
        print(f"t={t:6.1f}s  acc={a['tracking_accurate']} contra={a['contradicts_line']}  "
              f"{text[:52]!r}")
        print(f"          tracker: {a['_view'].replace('- view: ','')}")
        if a.get('tracker_error'):
            print(f"          tracker_error: {a['tracker_error']}")
        print(f"          {a['rationale']}")

    n = len(results)
    if n:
        acc = sum(r['tracking_accurate'] for r in results) / n
        contra = sum(r['contradicts_line'] for r in results) / n
        preventable = sum(1 for r in results if r['tracking_accurate'] and r['contradicts_line']) / n
        print(f"\n=== among {n} hallucinations ===")
        print(f"  tracker ACCURATE at that frame:        {acc:.0%}")
        print(f"  tracker facts CONTRADICT the line:     {contra:.0%}")
        print(f"  PREVENTABLE (accurate AND contradicts):{preventable:.0%}")
        print(f"  -> {1-acc:.0%} the tracker itself was wrong (needs Tier B/C, not prompt work)")
        out = BASE / f'hallu_tracking_audit_{variant}.json'
        json.dump({'variant': variant, 'n_hallucinations': n,
                   'tracker_accurate_rate': round(acc, 3),
                   'contradicts_rate': round(contra, 3),
                   'preventable_rate': round(preventable, 3),
                   'items': results}, open(out, 'w'), indent=2)
        print(f"\nwrote {out}")


if __name__ == '__main__':
    main()

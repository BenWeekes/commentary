#!/usr/bin/env python3
"""Standalone A/B: Claude (claude-opus-4-8) vs current OpenAI vision on REAL frame
bursts from the 5-min clip — latency + detection quality on known-hard moments.

Bursts are extracted fresh from the source mp4 (does not touch the live pipeline).
Moments include the reviewer-flagged perception errors:
  ~186-190s  yellow card (Kohn, Union)     — team attribution class
  ~200-204s  substitution (Sieb/Weiper on) — wrong-player class (was 'Tietz')
  ~276-280s  territory ('their own third' vs opponent's)
plus two ordinary-possession bursts as controls.

Usage:  .venv/bin/python test_claude_vision.py [--model claude-opus-4-8]
"""
import base64, json, os, re, subprocess, sys, time
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
SRC = Path('/tmp/v2v_compare/slice_5min.mp4')
OUT = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/claude_vision')
OUT.mkdir(parents=True, exist_ok=True)
for _l in open('/home/ubuntu/commentary/.env'):
    _l = _l.strip()
    if _l and '=' in _l and not _l.startswith('#'):
        k, _, v = _l.partition('=')
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

sys.path.insert(0, str(BASE))
import run_events_detector as D          # the production vision prompt + parser

BURSTS = {                                # label -> start seconds (4 frames, 0.55s apart)
    'card_188s': 186.5, 'sub_202s': 200.8, 'territory_278s': 276.5,
    'poss_60s': 60.0, 'poss_120s': 120.0,
}
CLAUDE_MODEL = 'claude-opus-4-8'
OAI_MODELS = ['gpt-5.4-mini', 'gpt-5.6']


def extract(label, t0):
    paths = []
    for i in range(4):
        p = OUT / f'{label}_{i}.jpg'
        if not p.exists():
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss',
                            f'{t0 + i * 0.55:.2f}', '-i', str(SRC), '-frames:v', '1',
                            '-vf', 'scale=960:540', '-q:v', '4', '-y', str(p)], check=True)
        paths.append(p)
    return paths


def prompt():
    return D.PROMPT_PATH.read_text()


def call_openai(model, paths):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    txt, ms, err = D.call_vision(client, model, paths, prompt())
    return txt, ms, err


def call_claude(model, paths):
    import anthropic
    client = anthropic.Anthropic()
    content = [{"type": "text", "text": prompt()}]
    for p in paths:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(p.read_bytes()).decode()}})
    t0 = time.monotonic()
    try:
        r = client.messages.create(
            model=model, max_tokens=800,
            output_config={"effort": "low"},           # latency-sensitive live loop
            messages=[{"role": "user", "content": content}])
        text = ''.join(b.text for b in r.content if b.type == 'text').strip()
        return text, int((time.monotonic() - t0) * 1000), None
    except Exception as e:
        return None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {str(e)[:200]}"


def summarize(raw):
    if not raw:
        return '(no output)'
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return raw[:120]
    try:
        d = json.loads(m.group(0))
    except Exception:
        return raw[:120]
    evs = [f"{e.get('type')}({e.get('team')},{e.get('confidence')})" for e in (d.get('events') or [])]
    p = d.get('possession') or {}
    return (f"events={evs or '-'} poss={p.get('team')}/{p.get('confidence')}"
            f" shirt#{p.get('player_shirt_number')}")


def main():
    model = CLAUDE_MODEL
    if '--model' in sys.argv:
        model = sys.argv[sys.argv.index('--model') + 1]
    results = {}
    for label, t0 in BURSTS.items():
        paths = extract(label, t0)
        row = {}
        for om in OAI_MODELS:
            txt, ms, err = call_openai(om, paths)
            row[om] = {'ms': ms, 'out': summarize(txt), 'err': err}
        txt, ms, err = call_claude(model, paths)
        row[model] = {'ms': ms, 'out': summarize(txt), 'err': err}
        results[label] = row
        print(f"\n== {label} (t={t0}s) ==")
        for m, r in row.items():
            print(f"  {m:<16} {r['ms']:>6}ms  {r['err'] or r['out']}")
    (OUT / 'results.json').write_text(json.dumps(results, indent=1))
    print(f"\nsaved {OUT}/results.json")
    lat = {m: sorted(r[m]['ms'] for r in results.values()) for m in list(OAI_MODELS) + [model]}
    print("\nlatency (ms) per model, sorted:", json.dumps(lat))


if __name__ == '__main__':
    main()

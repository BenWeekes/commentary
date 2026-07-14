#!/usr/bin/env python3
"""Harvest short, standalone, high-confidence Soniox utterances as a phrase pool.

From the token stream (start_ms/end_ms/confidence/is_final), segment into
sentences and keep only those that:
  - are <= MAX_DUR seconds long
  - finish on their own (end with . ! or ?)   [not a mid-sentence fragment]
  - have strong mean confidence (>= MIN_CONF)

These are the "easy" utterances that stand alone — the ones that broke before
were the long turns we split. Output -> soniox_short.jsonl for the eval page.

Usage: python harvest_soniox.py [max_dur=4] [min_conf=0.8]
"""
import json, os, re, sys
from pathlib import Path

for _l in open('/home/ubuntu/commentary/.env'):
    _l = _l.strip()
    if _l and not _l.startswith('#') and '=' in _l:
        _k, _, _v = _l.partition('='); os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
from openai import OpenAI
_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

TOKENS = Path('/home/ubuntu/commentary/experiments/v2v_5min_slice/soniox_v5_tokens.jsonl')
OUT = Path('/home/ubuntu/commentary/experiments/ai_commentator/soniox_short.jsonl')
MAX_DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
MIN_CONF = float(sys.argv[2]) if len(sys.argv) > 2 else 0.8
END_PUNCT = ('.', '!', '?')

RELEVANCE_SYSTEM = """You filter a football commentator's short utterances, keeping ONLY
those usable as live play-by-play commentary.

KEEP if it describes: on-pitch action, a player doing something, possession/passing,
a set piece, shot/save/tackle/header/cross/card/substitution, or useful match state
(time remaining, venue, scoreline).

DROP if it is: off-topic banter or personal chat, a tangent about names/history/anecdotes,
meta-conversation between the commentators, or a sentence FRAGMENT that only makes sense as
part of a longer sentence (e.g. "For Kohn.", "From Bayern.", "And Dr.", "The goal scorer.").

Return STRICT JSON: {"keep": [indices to keep]}."""


def relevance_filter(recs):
    """Keep only football play-by-play utterances (drop banter + fragments)."""
    if not recs:
        return recs
    numbered = "\n".join(f"{i}. {r['text']}" for i, r in enumerate(recs))
    try:
        resp = _client.responses.create(model='gpt-5.5', instructions=RELEVANCE_SYSTEM,
            input=[{"role": "user", "content": numbered}], max_output_tokens=2000,
            reasoning={"effort": "low"})
        m = re.search(r'\{.*\}', (resp.output_text or '').strip(), re.DOTALL)
        keep = set(json.loads(m.group(0)).get('keep', [])) if m else set(range(len(recs)))
    except Exception as e:
        print(f"relevance filter error ({e}); keeping all", file=sys.stderr)
        return recs
    return [r for i, r in enumerate(recs) if i in keep]


GOLD = Path('/home/ubuntu/commentary/experiments/ai_commentator/gold_soniox_5min.jsonl')
GOLD_SENTS = Path('/home/ubuntu/commentary/experiments/ai_commentator/gold_sentences.jsonl')

def split_sentences(text):
    return [p.strip() for p in re.split(r'(?<=[.!?])\s+', text.strip()) if p.strip()]

def main():
    # Source BOTH the phrase pool and the "Original" column from the SAME gold
    # transcript, at SENTENCE granularity with interpolated timestamps, so the
    # two columns on the page match exactly (same text, same times).
    turns = [json.loads(l) for l in open(GOLD)]
    all_sents = []
    for tr in turns:
        text = tr.get('text', '').strip()
        st, en = float(tr.get('start_s', 0)), float(tr.get('end_s', 0))
        words = text.split()
        if not words:
            continue
        rate = (en - st) / len(words) if en > st else 0.35   # seconds per word within the turn
        wi = 0
        for sent in split_sentences(text):
            n = len(sent.split())
            s_start = st + wi * rate
            s_end = st + (wi + n) * rate
            wi += n
            all_sents.append({'video_time_s': round(s_start, 2), 'end_s': round(s_end, 2),
                              'dur': round(s_end - s_start, 2), 'conf': 1.0, 'words': n,
                              'text': sent, 'speaker': tr.get('speaker', 0)})
    all_sents.sort(key=lambda r: r['video_time_s'])
    GOLD_SENTS.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in all_sents) + '\n')

    kept = [r for r in all_sents
            if 0 < r['dur'] <= MAX_DUR and r['text'].endswith(END_PUNCT) and r['words'] >= 2]
    n_pre = len(kept)
    kept = relevance_filter(kept)   # drop banter / tangents / fragments
    OUT.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in kept) + '\n')
    print(f"{len(all_sents)} gold sentences -> gold_sentences.jsonl; {n_pre} short+complete; "
          f"kept {len(kept)} after football-relevance filter (<= {MAX_DUR}s)")
    print(f"wrote {OUT}\n--- sample of the phrase pool ---")
    for r in kept[:14]:
        print(f"  {r['video_time_s']:6.1f}s ({r['dur']:.1f}s) {r['text']!r}")


if __name__ == '__main__':
    main()

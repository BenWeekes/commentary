#!/usr/bin/env python3
"""Harvest the LIVE-Soniox phrase pool: short, standalone, high-confidence
sentences straight from the token stream — carrying REAL per-sentence
confidence (unlike the gold-sourced pool, which is corrected -> conf=1.0).

This is the pool a live hybrid would actually use: name-boosted (Soniox was
fed the roster as context.terms) but imperfect, so confidence is the gate.

From soniox_v5_tokens.jsonl (start_ms/end_ms/confidence/is_final) we:
  - keep only 'original' final tokens (drop translations + <end>/<fin>)
  - stitch into sentences (split on . ! ?), timestamped by token times
  - keep sentences that are <= MAX_DUR s, end on their own, >= 2 words,
    with mean token confidence >= MIN_CONF
  - drop banter/fragments with the football-relevance LLM filter

Output -> soniox_live_short.jsonl  {video_time_s,end_s,dur,conf,words,text}
Usage: python harvest_soniox_live.py [max_dur=4] [min_conf=0.8]
"""
import json, os, re, statistics, sys
from pathlib import Path

for _l in open('/home/ubuntu/commentary/.env'):
    _l = _l.strip()
    if _l and not _l.startswith('#') and '=' in _l:
        _k, _, _v = _l.partition('='); os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
from openai import OpenAI
_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

TOKENS = Path('/home/ubuntu/commentary/experiments/v2v_5min_slice/soniox_v5_tokens.jsonl')
OUT = Path('/home/ubuntu/commentary/experiments/ai_commentator/soniox_live_short.jsonl')
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


def main():
    toks = [json.loads(l) for l in open(TOKENS)]
    fin = [t for t in toks
           if t.get('is_final') and t.get('translation_status') == 'original'
           and t.get('text', '').strip() not in ('<end>', '<fin>', '')]

    # stitch tokens into sentences, carrying token times + confidence
    sents, cur = [], []
    for t in fin:
        cur.append(t)
        if t['text'].strip().endswith(END_PUNCT):
            st = cur[0].get('start_ms', 0) / 1000.0
            en = cur[-1].get('end_ms', 0) / 1000.0
            conf = statistics.mean(x['confidence'] for x in cur)
            text = ''.join(x['text'] for x in cur).strip()
            sents.append({'video_time_s': round(st, 2), 'end_s': round(en, 2),
                          'dur': round(en - st, 2), 'conf': round(conf, 3),
                          'words': len(text.split()), 'text': text})
            cur = []
    sents.sort(key=lambda r: r['video_time_s'])

    kept = [r for r in sents
            if 0 < r['dur'] <= MAX_DUR and r['text'].endswith(END_PUNCT)
            and r['words'] >= 2 and r['conf'] >= MIN_CONF]
    n_pre = len(kept)
    kept = relevance_filter(kept)   # drop banter / tangents / fragments
    OUT.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in kept) + '\n')
    print(f"{len(sents)} live sentences; {n_pre} short(<= {MAX_DUR}s)+complete+conf>= {MIN_CONF}; "
          f"kept {len(kept)} after football-relevance filter")
    print(f"wrote {OUT}\n--- sample of the LIVE phrase pool (real confidence) ---")
    for r in kept[:14]:
        print(f"  {r['video_time_s']:6.1f}s ({r['dur']:.1f}s c{r['conf']:.2f}) {r['text']!r}")


if __name__ == '__main__':
    main()

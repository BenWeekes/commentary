# Television tennis commentary playbook

This benchmark studies official WTA full-match broadcasts to guide later
Glinka–Mayo versions. It does not provide grounding about the Cary match.

## Corpus

- Five official full-match sources, each sampled in three fixed five-minute
  windows: 75 minutes total.
- Four sources contain television commentary: Queen's Club grass, Madrid clay,
  Toronto hard court, and Indian Wells hard court. These provide 60 reference
  minutes.
- One Miami doubles source is a clean-world-feed control. Its 15 sampled
  minutes contain four court/player utterances and no booth commentary, so it
  is excluded from commentary cadence totals.
- Two sources use official English captions. Two commentary sources and the
  control use transient audio-only Deepgram STT.
- Source media and transcript text are never retained. The timestamped
  surrogate transcript stores only timing, word count, consensus category,
  commentary function, and a guarded paraphrase of at most 12 words. A
  four-word source-overlap check replaces unsafe paraphrases with fixed safe
  function notes.

## Headline comparison

| Measure | TV commentary references | Glinka–Mayo v2 |
|---|---:|---:|
| Sample | 60 min / 4 broadcasts | 5 min / 1 clip |
| Commentary turns per minute | 3.37 | 3.60 |
| Median words per turn | 17 | 8 |
| P90 words per turn | 53 | 10 |
| Speech / AI-voice occupancy | 43.0% | about 16% |
| Longest observed silence | 79.6 s | 32 s |

Caption/STT cues within one second are merged into a speech turn. A merged TV
turn can contain adjacent contributions from two commentators, so 53 words is
an observed upper-tail description, not a target for one generated line.

The cadence result is consistent across the four references:

| Reference | Commentator turns/min | Median words/turn |
|---|---:|---:|
| Maria–Anisimova, Queen's Club | 3.47 | 13 |
| Gauff–Swiatek, Madrid | 2.87 | 24 |
| Serena–Osaka, Toronto | 3.53 | 23 |
| Sabalenka–Rybakina, Indian Wells | 3.60 | 14 |

## What professional booths use

Among 202 consensus commentator turns:

| Primary content | Turns | Share |
|---|---:|---:|
| Match narrative or stakes | 44 | 21.8% |
| Tactics or pattern | 39 | 19.3% |
| Point reaction or outcome | 36 | 17.8% |
| Technique or shot | 28 | 13.9% |
| Score or server | 23 | 11.4% |
| Player background | 12 | 5.9% |
| Conditions or venue | 10 | 5.0% |
| Banter or other | 10 | 5.0% |

The functional view is similar: explaining the previous point (44), framing
stakes (42), and explaining tactics/technique (40) dominate. Pure score/server
statements account for 22 turns and live action calls for 17.

Professional tennis commentary therefore does not fill every rally with
play-by-play. It usually speaks between points, connects the last point to the
score situation, and adds analysis or narrative only when there is something
specific to say. Long silence is normal.

## Safe implications for Glinka–Mayo

The current pipeline can safely support:

- the accepted server and legal score transition;
- the player who won the point implied by that transition;
- game point, break point, deuce, advantage, hold, and changeover significance;
- verified pre-match facts, used sparingly;
- literal ball-in-play observations from the detector.

It cannot yet safely support tactics, shot type or quality, winner/error
attribution from vision, emotion, momentum, or physical-end identity after an
unverified change. Those remain blocked even though real booths use them
frequently.

After v2 review closes, the evidence supports a cautious v3 that:

1. keeps roughly the existing 18-call cadence rather than adding chatter;
2. expands selected calls toward 10–18 words, not the TV upper tail;
3. turns accepted score transitions into point-outcome plus score-significance
   lines;
4. combines literal rally evidence with the accepted server/score context;
5. reduces repetitive bare score/server phrasing;
6. does not add tactical, technical, emotional, or shot-quality claims until a
   detector can ground them.

## Reproduce

```bash
cd /home/ubuntu/commentary/experiments/tennis_commentator
/home/ubuntu/commentary/.venv/bin/python build_tv_corpus.py
```

The build checks that football is idle before each media/provider operation.
Safe per-source artifacts are resumable. Use `--refresh` only to regenerate
them from the official sources.

Artifacts:

- `tv_corpus_sources.json` — source/window manifest
- `benchmarks/tv_corpus/*.json` — per-source safe derived records
- `benchmarks/tv_corpus_summary.json` — aggregate cadence and categories
- `benchmarks/tv_corpus_paraphrases.jsonl` — timestamped surrogate transcript
- `build_tv_corpus.py` — reproducible downloader/analyzer

# Gemini Live vs current pipeline — 5-minute comparison

Source: `m05_uni_eval_25min/source.mp4`, window **300 s – 600 s** (5 minutes, real Bundesliga commentary)
Gold transcript: `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/`

## Headline result

| Metric | Soniox (current) | Gemini Live |
|---|---|---|
| English transcript: WER vs gold | **5.9 %** | **80.8 %** |
| English transcript: words emitted | 566 (vs 562 gold) | **137 (24 % coverage)** |
| French audio produced | n/a (we use ElevenLabs) | **57 s for 300 s source = 19 % coverage** |
| French translation quality (sample) | known | sentences read natural where present, **cut off mid-word** at turn boundaries |

**The gap is not WER — it's coverage.** Gemini Live emits only ~1 in 4 spoken words and ~1 in 5 seconds of French audio when fed live continuous football commentary. Most of the WER (454 of 562 edits) is *deletion*: Gemini simply didn't say things that were said.

## What's in the listening files

| URL | What you hear |
|---|---|
| http://sip.dev.gw.01.agora.io/v2v_source_5min.mp4 | Original English commentary, full 5 min |
| http://sip.dev.gw.01.agora.io/v2v_gemini_only.mp4 | Only the 57 s that Gemini produced — video truncated to match the audio |
| http://sip.dev.gw.01.agora.io/v2v_gemini_5min.mp4 | 5 min with **English on the left channel and Gemini French on the right** — most striking demonstration of the gaps |

## Sample French output (first 500 chars)

```
Khedira mène cette charge en particulier. À première vue, je suis d'accord
avec l'arbitre. charge en particulier. À premièreÀ première vue, je suis
d'accord avec l'arbitre. J'aimerais voir un autre angle. Je pense que c'est
une course pour le ballon et Jae-sung Lee arrive juste avant Jeong, donc coup
franc accordé à FSV Mainz. Décision correcte. Kawasaki qui va entrer pour
Mainz. 13 minutes de— Ils ont créé de la maîtrise mais c'est— Oui. Eh bien,
il n'y a absolument aucune ressemblance entre Derric…
```

Notice:
- The opening (`Khedira mène cette charge en particulier…`) is correctly translated and idiomatic.
- Soon: **stutter repeats** ("charge en particulier. À premièreÀ première vue").
- **Cut-offs at turn boundaries**: `13 minutes de—`, `c'est—`, `Derric` (cut mid-word in "Derrick").
- After this point most of the source goes untranslated entirely.

## What this means in practice

Gemini Live behaves well on **short, well-spaced utterances** (the 30 s probe earlier looked fine). On **dense continuous commentary**, the model's turn-management cuts it off repeatedly: each "interrupted" event in the event log represents a turn that was abandoned because new input arrived. Across the 5 minutes there were:

- 15 turn_complete events
- 11 generation_complete events
- 7 interrupted events
- Total: model started, produced a few words, got cut off, started again — losing the bulk of the content.

This is not a tunable. It's a property of how the model handles input that arrives faster than it can render audio output.

## Implications for the architectures we discussed

| Architecture | Status after this evidence |
|---|---|
| Full Gemini v2v (audio→audio replacement) | **Not viable** for live football. 19 % audio coverage = listener gets silence most of the time. |
| Hybrid: Gemini transcript → ElevenLabs | **Not viable for now.** 23 % transcript coverage means we'd only synthesize 1 of every 4 sentences. Same content-loss problem, just with our voices. |
| Current: Soniox + gpt-5.5 + ElevenLabs | **Stays the best option.** Soniox at 5.9 % WER, full coverage, ElevenLabs voices we've tuned. |

## What might recover Gemini's viability

Worth knowing, in case product needs change:

1. **Different Gemini model.** Tested model is `gemini-3.1-flash-live-preview`. The closed-beta `gemini-3.1-flash-lite-live-translate` (the streaming-translation-specific variant referenced in the leaked spec) might handle continuous input differently. We don't have a key for it.
2. **Stop asking for audio output** — set `responseModalities: ["TEXT"]` only. Without TTS rendering, the model may keep up better. Trades v2v for "Gemini-translated text only", but if coverage rises, that's the hybrid path back.
3. **Slow the audio stream down** — feed at < 1× real-time. Not usable for live broadcast but proves whether the model is throughput-limited or design-limited.
4. **Test on lower-density material** (e.g. interview-style, not match commentary) to characterise where the threshold is.

## Files

- `/tmp/v2v_compare/gemini_events.jsonl` — full event log with timestamps
- `/tmp/v2v_compare/gemini_en_full.txt` — Gemini's English transcript
- `/tmp/v2v_compare/gemini_fr_full.txt` — Gemini's French transcript
- `/tmp/v2v_compare/gemini_fr_audio.wav` — raw Gemini French PCM (24 kHz mono)
- `/tmp/v2v_compare/soniox_en_300_600.txt` — Soniox transcript for the same window
- `/tmp/v2v_compare/gold_en_300_600.txt` — gold transcript for the same window
- `/tmp/v2v_compare/wer_report.txt` — WER summary

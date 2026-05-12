# STT Quality Experiment: May 10 Match

This document is intended for review by another engineering session. It keeps the current background, then records the method and results of the STT comparison as an experiment rather than a final production decision.

## 1. Background

Customer feedback from the May 10 `m05_uni_md33` match:

- many sentences were cut before the end
- pauses between sentence parts were too long
- terminology was sometimes out of context
- some words sounded random or invented
- commentator and expert speech was sometimes mixed together

Earlier analysis found that some "random" translated words were faithful translations of bad English STT, not necessarily translation hallucinations. Examples included plausible football-context mishears and broken fragments. That makes STT recognition, turn detection, diarization, and phrase boundaries the first things to improve.

Important precondition: quality should only be judged after the live playback anchor fix is present:

- `2daf642 fix(live): anchor stt playback to audio feed`

Before that fix, translated audio could play around 7 seconds after the corresponding video. With that sync error, even good STT/translation can feel wrong.

## 2. Research Question

For live football commentary translation, which realtime STT setup gives the best input to the translation/TTS pipeline?

The target is not simply lowest WER. A useful live STT setup should also:

- avoid cutting sentences or clauses in the middle
- finalize quickly enough to leave translation/TTS time inside the video delay
- preserve commentator vs expert speaker turns
- recognize football names and terms
- avoid feeding the translator tiny fragments such as `for`, `but`, or `Football here in`

Secondary research question:

Can a streaming translation / streaming TTS pipeline reduce end-to-end latency without hurting football translation quality?

Candidate alternatives to compare after the STT bake-off:

- Current pipeline: realtime STT -> GPT-5.4 translation -> ElevenLabs TTS clip generation -> optional ffmpeg speed-fit
- Soniox streaming translation: realtime audio -> Soniox transcription + translated text tokens -> ElevenLabs TTS
- GPT streaming translation + ElevenLabs text-streaming TTS: realtime STT -> streaming GPT translation tokens -> ElevenLabs WebSocket input streaming -> streamed speech output

## 3. Hypotheses

Initial hypotheses:

1. Soniox realtime may outperform Deepgram Nova-3 on this football audio if integrated correctly.
2. Longer endpoint delays may reduce sentence fragments by waiting for more context.
3. Longer endpoint delays might increase accuracy, but at the cost of latency.
4. If longer turns improve STT quality but reduce pipeline budget, a longer video delay may be preferable to forcing shorter STT turns.
5. Roster-only name correction can be applied after STT and before translation, but raw STT must be retained for provider evaluation.
6. Soniox streaming translation may reduce translation latency, but it must be judged against GPT-5.4 for meaning, football terminology, style, and name correction.
7. ElevenLabs WebSocket input streaming may reduce TTS time-to-first-audio, but committing speech before the translation phrase is stable could worsen prosody or make corrections impossible.

## 4. Benchmark Data

Use the manually reviewed 25-minute section from:

- Match: `m05_uni_md33`
- Run: `20260510_190915`
- Gold transcript: `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json`
- Review page: `https://sip.dev.gw.01.agora.io/stt_eval_m05_uni_md33.html`
- Source WAV: `match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav`
- Live-style MP4: `clips/m05_uni_eval_25min/source.mp4`
- Keyterms: `match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt`

The gold transcript was built from the actual match audio and reviewed for obvious player-name issues. Treat it as the current reference unless colleagues provide concrete corrections.

Do not use Deepgram's original live output as the reference for judging Deepgram or Soniox.

## 5. Integration Fix Before Measuring Soniox

The previous Soniox realtime result was invalid for latency. The old integration used an async sender loop that read/slept on audio chunks inside the event loop. That starved `recv()` and made Soniox appear to emit turns tens of seconds late, even though raw provider messages showed final tokens arriving much earlier.

Local fixes made for this experiment:

- `lib/soniox_stt_pipeline.py`
  - use a synchronous WebSocket plus a separate sender thread
  - keep the receiver loop unblocked
  - use `audio_format: "pcm_s16le"`
  - treat Soniox as a final-token stream
  - buffer final tokens and emit on `<end>` / `<fin>`
- `tools/run_live_stt_eval.py`
  - use the same nonblocking Soniox design in the standalone evaluator
  - add config sweeps for Deepgram Nova and Soniox
  - add split/fragment metrics in addition to WER

Syntax check:

```bash
.venv/bin/python -m py_compile tools/run_live_stt_eval.py lib/soniox_stt_pipeline.py
```

## 6. Method

The evaluation streams the same 16 kHz mono WAV at realtime pace to each provider/configuration.

Full sweep command:

```bash
.venv/bin/python tools/run_live_stt_eval.py \
  --providers nova,soniox \
  --nova-configs 500:1500:8,700:2000:10,1000:2500:12 \
  --soniox-endpoints 700,1000,1500,2500,4000 \
  --out match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512
```

Output artifacts:

- Summary: `match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512/summary.md`
- Per-provider `turns.json` and `score.json` files under the same directory

The sweep was run concurrently across configurations. That is useful for a quick comparison, but a reviewer should re-run top candidates sequentially to rule out provider/rate-limit effects.

This experiment only measured realtime STT. It did not compare:

- Soniox streaming translation quality or latency
- GPT-5.4 streaming translation
- ElevenLabs text-input/audio-output streaming
- end-to-end translated-audio latency

## 6a. Reproducibility Steps For Review

Start from the repo root:

```bash
cd /home/ubuntu/commentary
```

Check required local inputs exist:

```bash
test -f match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav
test -f match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json
test -f match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt
test -f /home/ubuntu/soniox
test -f .env
```

Confirm keys are available:

```bash
grep -q '^DEEPGRAM_API_KEY=' .env
test -s /home/ubuntu/soniox
```

Run syntax validation:

```bash
.venv/bin/python -m py_compile tools/run_live_stt_eval.py lib/soniox_stt_pipeline.py
```

Optional quick smoke test, about 60 seconds:

```bash
.venv/bin/python tools/run_live_stt_eval.py \
  --audio match_data/m05_uni_md33/eval/20260510_190915/live_stt_smoke/smoke_60s.wav \
  --providers nova,soniox \
  --nova-configs 500:1500:8,700:2000:10,1000:2500:12 \
  --soniox-endpoints 700,1000,1500 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_smoke_$(date -u +%Y%m%d_%H%M%S)
```

Run the full concurrent sweep, about 25-27 minutes:

```bash
.venv/bin/python tools/run_live_stt_eval.py \
  --providers nova,soniox \
  --nova-configs 500:1500:8,700:2000:10,1000:2500:12 \
  --soniox-endpoints 700,1000,1500,2500,4000 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_full_$(date -u +%Y%m%d_%H%M%S)
```

Expected output files in the chosen output directory:

```text
summary.md
summary.json
soniox_rt_endpoint700/turns.json
soniox_rt_endpoint700/score.json
soniox_rt_endpoint700/raw.jsonl
soniox_rt_endpoint1000/turns.json
...
deepgram_nova3_ep500_utt1500_max8/turns.json
deepgram_nova3_ep500_utt1500_max8/score.json
...
```

The historical full sweep to compare against is:

```text
match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512/summary.md
```

To validate the odd Soniox endpoint result, re-run top candidates sequentially, one at a time:

```bash
.venv/bin/python tools/run_live_stt_eval.py \
  --providers soniox \
  --soniox-endpoints 700 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_seq_soniox700_$(date -u +%Y%m%d_%H%M%S)

.venv/bin/python tools/run_live_stt_eval.py \
  --providers soniox \
  --soniox-endpoints 1000 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_seq_soniox1000_$(date -u +%Y%m%d_%H%M%S)

.venv/bin/python tools/run_live_stt_eval.py \
  --providers soniox \
  --soniox-endpoints 1500 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_seq_soniox1500_$(date -u +%Y%m%d_%H%M%S)

.venv/bin/python tools/run_live_stt_eval.py \
  --providers nova \
  --nova-configs 500:1500:8 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_seq_nova_current_$(date -u +%Y%m%d_%H%M%S)

.venv/bin/python tools/run_live_stt_eval.py \
  --providers nova \
  --nova-configs 700:2000:10 \
  --out match_data/m05_uni_md33/eval/20260510_190915/review_seq_nova_looser_$(date -u +%Y%m%d_%H%M%S)
```

Review checklist:

1. Compare each new `summary.md` against the historical table.
2. Open each provider's `score.json` and inspect the `worst` rows.
3. For those worst rows, compare the gold text, provider text, and timestamps in `turns.json`.
4. Decide whether high WER is caused by actual wrong words or by boundary/window scoring.
5. Check raw Soniox timing in `raw.jsonl`; final token messages should arrive close to the reported `stt_latency_ms`, not tens of seconds later.
6. If sequential results differ materially from the concurrent sweep, prefer sequential results.

Recommended method improvements for the reviewer:

- Add a global transcript WER that ignores turn boundaries.
- Add a boundary F1 score: compare provider boundary times against gold boundaries within 500ms/1000ms.
- Add a speaker-label accuracy score against gold speaker labels.
- Add a human "usable for translation" label for 30-50 representative windows.
- Re-score with and without provider words outside each gold window to quantify insertion penalties.

## 7. Metrics

The evaluator reports:

- `Turns`: emitted STT turns
- `Median Turn`: median source-audio duration per turn
- `Short`: turns shorter than 1 second
- `Frag-like`: heuristic count of turns that look incomplete, such as ending with comma/preposition/conjunction/article
- `Split Gold`: percentage of gold turns overlapped by more than one provider turn
- `Median Latency` / `P90 Latency`: time from source audio end to STT final emission
- `WER`: word error rate against overlapped gold windows
- `Similarity`: rough text similarity against gold windows

### How WER Is Currently Computed

Current scoring code: `tools/run_live_stt_eval.py::score_turns()`.

For each gold turn:

1. Define a time window from `gold.start - 0.5s` to `gold.end + 0.5s`.
2. Find every provider turn whose `[start, end]` overlaps that window.
3. Concatenate those provider turn texts in order.
4. Normalize both gold and provider text:
   - lowercase
   - remove punctuation/non-alphanumeric characters
   - split on whitespace
5. Compute word-level Levenshtein edit distance.
6. Add edits and reference word counts across all scored gold turns.
7. Report `WER = total_edits / total_gold_words`.

Pseudocode:

```python
for gold_turn in gold:
    window = [gold.start - 0.5, gold.end + 0.5]
    hyp_text = " ".join(
        provider_turn.text
        for provider_turn in provider_turns
        if provider_turn.end >= window.start
        and provider_turn.start <= window.end
    )
    edits += levenshtein(words(gold_turn.text), words(hyp_text))
    ref_words += len(words(gold_turn.text))

wer = edits / ref_words
```

Metric caveats:

- `WER` is sensitive to segmentation and window overlap. It can penalize a provider for different boundaries even when the words are acceptable.
- `Frag-like` is a heuristic, not a human judgment.
- The gold transcript was derived from Soniox offline/improved output, so there may be provider-family bias.
- Speaker quality is not yet summarized numerically in the table.
- The current WER is not a true global transcript alignment. It is a per-gold-turn overlap-window score.
- A provider with shorter turns can sometimes score better because the overlap window captures fewer unrelated words before/after the gold turn.
- A provider with longer turns can sometimes score worse because one long provider turn overlaps multiple gold turns; repeated inclusion of surrounding words can inflate insertions.
- Therefore the result "shorter Soniox endpoint has lower WER" may mean "better word recognition", "better temporal localization for this scorer", or both.

### Why Shorter Turns Might Appear More Accurate

There are two possible categories:

Real provider behavior:

- Earlier endpoints may prevent Soniox from merging across speaker changes or long pauses.
- Shorter turns may keep token context closer to the acoustic phrase being finalized.
- Longer endpoint delays may let more partial/final token revision happen, and Soniox may occasionally revise in the wrong direction.

Scoring artifact:

- The gold transcript has its own phrase boundaries.
- The scorer matches by time overlap, not by optimal transcript alignment.
- If a provider emits a long turn that spans two gold turns, the same long text can be compared against each gold turn separately, creating extra insertion errors.
- If a provider emits short turns, only the local words are pulled into each gold window, which can reduce insertion errors even if the human translation experience is choppier.

This is why the WER table must be validated with:

- sequential re-runs of top configs
- global transcript alignment independent of turn boundaries
- a separate boundary/fragment score
- human review of representative windows

## 8. Results

The table below keeps the original window score and adds Claude's independent global WER rescore from:

- `match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512/claude_rescore.json`

`Window WER` is segmentation-coupled and should no longer be treated as the primary accuracy metric. `Global WER` concatenates each transcript in time order and ignores turn boundaries, so it is a better measure of raw word accuracy.

| Provider / Config | Turns | Median Turn | Short | Frag-like | Median Latency | P90 Latency | Window WER | Global WER | Dedup WER |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soniox `endpoint=700ms` | 492 | 1.38s | 179 | 81 | 606ms | 832ms | 0.204 | 0.075 | 0.083 |
| Soniox `endpoint=1000ms` | 347 | 2.10s | 77 | 52 | 607ms | 1661ms | 0.237 | 0.061 | 0.074 |
| Soniox `endpoint=1500ms` | 310 | 2.22s | 79 | 23 | 607ms | 1068ms | 0.345 | 0.054 | 0.110 |
| Soniox `endpoint=2500ms` | 263 | 2.40s | 70 | 14 | 610ms | 1806ms | 0.610 | 0.054 | 0.220 |
| Soniox `endpoint=4000ms` | 255 | 2.64s | 64 | 13 | 613ms | 1730ms | 0.674 | 0.052 | 0.231 |
| Deepgram Nova `700/2000/max10` | 327 | 4.13s | 2 | 88 | 2545ms | 3555ms | 0.589 | 0.150 | 0.295 |
| Deepgram Nova `1000/2500/max12` | 323 | 4.13s | 1 | 85 | 2577ms | 3605ms | 0.599 | 0.151 | 0.299 |
| Deepgram Nova `500/1500/max8` | 333 | 4.07s | 6 | 93 | 1935ms | 3019ms | 0.610 | 0.157 | 0.304 |

Observed:

- Soniox was materially faster than Deepgram Nova in this benchmark.
- Soniox was materially more accurate than Deepgram Nova on global WER.
- Within Soniox, endpoint setting barely changed global WER compared with the earlier window-WER ranking.
- Soniox `700ms` had many short turns; this may be bad for translation despite good raw word accuracy.
- Soniox `1500ms+` reduced heuristic fragments without materially harming global word accuracy.
- Nova configs produced longer turns, but still many fragment-like endings and worse global WER.

## 9. Corrected Interpretation

Claude independently validated the scoring concern and reproduced the artifact with a synthetic self-test. A perfect transcript emitted as one long provider turn can score badly under the old window WER because the same long text is compared against multiple gold windows and counted as insertions.

Corrected conclusions:

- The "shorter endpoint = lower WER" result was primarily a scoring artifact.
- Soniox endpoint choice should be made on translation usability and latency budget, not the old window WER.
- Global WER is roughly flat across Soniox endpoints, with all tested Soniox configs around 5-8% WER.
- Deepgram Nova remains materially worse on this benchmark, around 15-16% global WER.

Practical implication:

- `700ms` is likely too choppy for translation even though global WER is good.
- `1500ms` is now a stronger live-demo candidate than `1000ms` because it greatly reduces fragment-like turns while keeping global WER essentially equal.
- `2500ms` or `4000ms` may be worth human review, but their longer endpointing must be checked against live translation/TTS timing and speaker-turn behavior.
- The next decision should be based on human review of gold-vs-realtime comparison pages and live-demo recordings, not only automated WER.

## 10. Delay Budget Analysis

Approximate remaining budget after source turn duration and STT finalization:

| Candidate | Video Delay | P5 Budget | P10 Budget | Median Budget | Turns Under 3s Budget |
|---|---:|---:|---:|---:|---:|
| Soniox `700ms` | 14s | 9.06s | 9.71s | 11.97s | 1 / 492 |
| Soniox `1000ms` | 14s | 5.72s | 7.41s | 11.17s | 6 / 347 |
| Soniox `1500ms` | 14s | 4.70s | 6.22s | 10.93s | 11 / 310 |
| Nova `500/1500/max8` | 14s | 4.79s | 4.99s | 7.92s | 1 / 333 |
| Nova `700/2000/max10` | 14s | 2.71s | 7.29s | 7.37s | 22 / 327 |
| Nova `700/2000/max10` | 16s | 4.71s | 9.29s | 9.37s | 0 / 327 |

Interpretation:

- Soniox `1500ms` appears usable with the current 14s delay for most turns.
- Soniox `2500ms` may also be usable, but should be checked with live-demo recordings and TTS budget.
- Longer Nova configs likely need 16s delay for comfortable translation/TTS budget.
- If TTS drops remain high, increasing video delay to 16s may be cleaner than increasing Soniox endpoint delay.

## 11. Working Recommendation For Live Demo

Use this only as the next validation candidate:

- STT provider: Soniox realtime `stt-rt-v4`
- `max_endpoint_delay_ms=1500`
- video delay: keep 14s initially
- keyterms: surname plus `firstname surname`
- add a separate roster-only LLM name correction pass before translation

Keep raw STT and corrected STT separate:

- raw STT judges provider accuracy
- corrected STT judges the text used for translation

The name-correction pass must only fix roster/team/venue/referee names. It must not rewrite football actions, score state, tactics, or sentence meaning. Log every correction.

## 12. Validation Tasks For Another Session

1. Re-run top candidates sequentially:
   - Soniox `700ms`
   - Soniox `1000ms`
   - Soniox `1500ms`
   - Nova `500/1500/max8`
   - Nova `700/2000/max10`
2. Inspect worst WER rows for Soniox `700/1000/1500` to decide whether `1500ms` really loses words or is only boundary-penalized.
3. Add a human "turn usable for translation" judgment on a representative subset.
4. Score speaker labels against the gold transcript, especially commentator/expert handoffs.
5. Test a post-STT merge policy:
   - keep fast Soniox endpointing
   - hold and merge adjacent short/incomplete turns only when delay budget allows
   - never merge across speaker changes unless manually proven safe
6. Run the chosen candidate through `m05_uni_eval_demo` / `demo_srt_direct` to validate live scheduling, `intended_skew_ms`, translation, TTS, and Agora playback.

## 13. Translation And TTS Streaming Experiment

This should be a separate experiment from STT selection. The STT experiment determines the best English source transcript and turn boundaries. The translation/TTS experiment determines how quickly and accurately we can turn that source into spoken target-language commentary.

Compare at least these arms:

| Arm | Translation | TTS | Purpose |
|---|---|---|---|
| Current baseline | GPT-5.4 full-turn translation | ElevenLabs full text -> audio clip | Quality baseline and current production behavior. |
| GPT streaming | GPT-5.4 streaming translation tokens | ElevenLabs WebSocket text input + streamed audio output | Test whether translation/TTS can overlap safely. |
| Soniox translation | Soniox streaming translated text tokens | ElevenLabs WebSocket text input + streamed audio output | Test lower latency and integrated speech translation. |
| Soniox translation, buffered | Soniox translated text, but buffered until stable phrase boundary | ElevenLabs full or streaming TTS | Test quality/latency tradeoff if raw streaming translation is too unstable. |

Metrics to record:

- source STT final time
- first translated token time
- translated text final time
- first audio byte time
- playable-audio-ready time
- final audio duration
- whether local `atempo` speed-fit was still needed
- translation faithfulness against source
- football terminology and name correctness
- whether streaming output revised earlier meaning
- TTS prosody and whether phrase boundaries sound natural

Important controls:

- Use identical STT turns for all translation/TTS arms.
- Keep raw STT and name-corrected STT separate.
- Run the roster-only name correction before translation for GPT and Soniox arms if that is intended production behavior.
- Do not let a streaming system start speaking text that may be semantically revised unless the experiment explicitly measures that risk.

Notes from docs checked on 2026-05-12:

- Soniox WebSocket API supports realtime transcription and translation of live audio.
- ElevenLabs WebSocket TTS supports bidirectional streaming: incremental text input and streamed audio output.
- ElevenLabs warns that sending text incrementally can reduce latency, but committing to audio before enough text is available can harm natural phrase boundaries.

Open question:

- Soniox streaming translation may be excellent for latency, but GPT-5.4 may still be better for football naturalness, roster-aware correction, and conservative handling of STT uncertainty. This must be measured, not assumed.

## 14. Initial Translation Experiment Results

Run date: 2026-05-12.

Evaluator added:

```bash
.venv/bin/python tools/run_translation_eval.py \
  --lang es \
  --duration 120 \
  --out match_data/m05_uni_md33/eval/20260510_190915/translation_eval_es_120s_20260512_113146

.venv/bin/python tools/run_translation_eval.py \
  --lang de \
  --duration 60 \
  --out match_data/m05_uni_md33/eval/20260510_190915/translation_eval_de_60s_20260512_113505
```

Important implementation note:

- Soniox realtime translation did not behave correctly with the old plain-STT raw audio format label `pcm_s16le`.
- Using the documented raw format label `s16le`, with `translation: {type: one_way, target_language: ...}`, produced interleaved original and translated tokens.
- The evaluator therefore uses `s16le` for translation experiments while the production Soniox STT-only path still uses the existing STT integration.

Measured latency:

| Language | Duration | Soniox turns | GPT turns | Soniox translation median / p90 | GPT translation median / p90 |
|---|---:|---:|---:|---:|---:|
| ES | 120s | 24 | 26 | 740ms / 2158ms | 1627ms / 3941ms |
| DE | 60s | 9 | 10 | 749ms / n/a | 1804ms / 3158ms |

Qualitative result:

- Soniox streaming translation is materially faster.
- Raw Soniox translated text is not yet good enough as a drop-in replacement for GPT full-turn translation on this sample.
- The failure mode is not only style; it includes dropped/mangled words inside translated turns and occasional source/translation alignment shift.

Examples:

| Source | Soniox streaming translation | GPT full-turn translation | Assessment |
|---|---|---|---|
| "Still a bite to this game, and the fans, of course, playing their part." | ES: "Sigue siendo un golpe para este partido, y los aficionados, por supuest haci lo suyo." | ES: "Todavía hay intensidad en este partido, y los aficionados, por supuesto, poniendo de su parte." | Soniox is faster but has garbled words and a bad football idiom. |
| "I think it is what differentiates German football..." | DE: "was einen deutschen Fußball unterscheidet von in so viel anderen Teilen..." | DE: "was den deutschen Fußball ... vom Fußball in so vielen anderen Teilen..." | GPT keeps grammar and meaning better. |
| "Fans here are not customers." | ES/DE both good in Soniox and GPT. | ES/DE both good. | Short, complete turns are fine. |

Current conclusion:

- Keep GPT-5.4 full-turn translation as the quality baseline.
- Do not route production spoken commentary directly from raw Soniox streaming translation yet.
- The more promising near-term use of Soniox remains realtime STT plus diarization/endpointing, followed by roster-only name correction and GPT full-turn translation.
- Soniox streaming translation can still be useful for a future low-latency subtitle/caption mode, or as an auxiliary signal for translation confidence, but it needs broader human review before TTS output.

Next translation/TTS experiment:

- Test GPT streaming translation into ElevenLabs streaming TTS.
- The key question is whether GPT can preserve full-turn quality while overlapping translation and TTS enough to reduce playable-audio-ready time.
- If streaming GPT is unstable, test a buffered hybrid: wait for a stable clause, then stream that clause to ElevenLabs while the rest of the turn continues.

## 15. Production Work Not Yet Done

Do not switch production solely from this experiment. Remaining work:

- expose/use Soniox endpoint config in match config; current live-demo candidate is `max_endpoint_delay_ms=1500`
- add roster-only name correction before translation
- preserve raw and corrected STT in logs
- add translation/TTS arm identifiers to logs so GPT vs Soniox translation and full-clip vs streaming TTS can be compared
- surface `speaker`, correction audit, short/fragment stats, and provider config in status/detail views
- add a live-demo smoke test that asserts `intended_skew_ms` remains near zero

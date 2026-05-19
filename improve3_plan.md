# Translation Safety And Quality Plan

## Goal

Prevent bad translation output from reaching TTS, especially assistant-style refusal text and literal football idiom translations, without changing STT segmentation or sentence-continuity behavior in the same step.

## Phase 1 — Safety And Prompt Contract

Implement first and test on the first 7 minutes of `m05_uni_eval_demo`.

- Log translation race attempts for primary and fallback:
  - model
  - latency
  - success/error
  - guard decision
  - selected model/reason
  - output preview for rejected/debug inspection
- Add a universal output guard before TTS:
  - reject empty output
  - reject `__TRANSLATION_FAILED__`
  - reject assistant/refusal boilerplate
  - reject extreme output/input length ratio
  - if rejected, prefer a valid primary result if available within a short extra wait
  - otherwise return empty output so the utterance is dropped rather than spoken
- Improve both translation prompts:
  - translate meaning faithfully, not word-for-word
  - render football idioms naturally
  - never invent players, actions, events, score state, or tactical detail
  - never apologize, explain, answer, or refuse
  - if translation cannot be produced, return exactly `__TRANSLATION_FAILED__`
  - preserve fragment structure for partial utterances

Expected impact:

- Fix refusal leakage such as `Work out.` -> `Je suis désolé...`
- Improve idioms such as `Caught every day of the week by Carl Klaus`
- Keep bad or suspicious output silent instead of audible

## Phase 2 — Model Choice

Run a broader translation eval comparing `gpt-5.5`, `gpt-5.4`, and `gpt-4o-mini` after Phase 1. Current live eval config promotes `gpt-5.5` to primary and uses `gpt-5.4` as fallback.

Measure:

- p50/p90 latency
- refusal/guard rejection rate
- idiom quality
- fragment quality
- drop impact under the live delay budget

Only switch the server primary model if the latency budget and output quality both improve.

### Reproduce 5.4 vs 5.5 from Existing Logs

Use a completed run such as `match_data/m05_uni_eval_demo/runs/20260514_082830`.

1. Load `fr.jsonl` and collect `source="stt"` rows from the first target window, e.g. `audio_start <= 430`.
2. Always include the known probe inputs:
   - `Work out.`
   - `Caught every day of the week by Carl Klaus. See what he's trying here.`
   - `and.`
   - `But at the same time, Union have.`
   - `Not been able to.`
3. For each source text, call the same prompt through `lib.translator.translate_text(...)` with:
   - `model="gpt-5.5", reasoning_effort="low"`
   - `model="gpt-5.4", reasoning_effort="low"`
   - older fallback model, e.g. `model="gpt-4o-mini", reasoning_effort=None`
4. Run each output through `lib.translator.guard_translation_output(source, output, "fr")`.
5. Report per model:
   - accepted/rejected count
   - rejection reasons
   - latency avg/p50/p90
   - manual quality notes for fragments, idioms, name preservation, and hallucinated context
6. Do not switch primary/fallback from a micro-test alone. Use the results to pick candidates, then rerun the 7-minute live eval and compare `fully_played_pct`, guard rejection rate, drops, and audible quality.

## Phase 3 — Previous-Utterance Context

Implemented behind `translation_context_enabled`. **A/B result: did not improve quality; kept as opt-in, disabled by default.**

Added previous English and previous target-language translation as optional context per language. Context is selected by source audio time rather than translation completion time so parallel preparation cannot feed future utterances into the prompt. History is bounded per-language (`deque(maxlen=24)`), same-speaker filtered, and excludes empty/dropped translations so bad anchors don't propagate.

### A/B result (run `20260514_170607` vs best `20260514_154918`)

| lang | best | ctx | Δ |
|---|---:|---:|---:|
| EN | 95.3% | 94.5% | -0.8 pp |
| ES | 95.0% | 93.2% | -1.8 pp |
| PT | 85.4% | 85.5% | +0.1 pp |
| FR | 92.9% | 91.9% | -1.0 pp |
| TR | 93.8% | 93.5% | -0.3 pp |
| DE | 92.5% | 91.6% | -0.9 pp |

Plus FR p90 translation latency rose 2097 → 2588 ms (+490 ms) and p95 rose 2433 → 3122 ms (+689 ms) — roughly 25% increase from the larger prompt.

### Why it did not fix the target cases

The `"Work out."` case was the canonical motivating example. With context enabled, Soniox segmented the speech as:

- `270.8s` `"Union Jeong."`
- `272.1s` `"And France did it."`
- `274.3s` `"Work out."`

The actual continuous English was `"Woo-yeong Jeong and pass didn't work out."` — but Soniox had already mis-split it into three garbled chunks **before** our pipeline ever saw it. So the previous-utterance context fed to the model for `"Work out."` was `"And France did it."`, which has no semantic connection to it. The model still produced `"Fonctionner."` — the same wrong-direction translation as without context.

The general principle: prev-context only helps when STT segments along clean sentence boundaries. The exact cases we wanted to fix (`"Work out."`, `"Remaining."`, `"Not been able to."`) are *by definition* cases where Soniox mis-segmented — so the preceding chunk is also garbled and provides no useful context. The fix targets the wrong layer.

### Status and forward-looking note

`translation_context_enabled` stays in place as an opt-in flag, defaulting to `false`, available for future controlled experiments where a different prompt shape, larger context window, or different STT-segmentation regime might justify revisiting.

**Do not re-attempt prev-context expecting a different result without first changing one of:**

- the STT segmentation regime (so the previous utterance is actually the prior speech), or
- the form of context passed (e.g. include the live source audio acoustically, not just text)

## Phase 5 — Address Fragment Translation At The Right Layer

The unfixed quality issue is fragment translations going in the wrong direction (`"Work out."` → `"Fonctionner."` instead of `"Ça n'a pas marché."`). Phase 3 proved this cannot be fixed at the translation layer alone given current Soniox segmentation. Three candidate experiments, each at a different layer.

### 5a. STT-side fragment merging

Buffer same-speaker short STT utterances for a small window (e.g. up to 600 ms) before emitting. If a continuation arrives, merge into a single utterance before passing to translation. If not, emit the original fragment alone.

- **Pro:** Surgical — only touches short fragments, leaves normal utterances unchanged.
- **Pro:** Restores the missing context at the layer where it was lost.
- **Con:** Adds a 200-600 ms buffering delay on every short utterance. Eats into the `video_delay` budget.
- **Con:** Requires a "is this a continuation?" heuristic, which has its own failure modes.
- **Effort:** Medium. Lives in `lib/soniox_stt_pipeline.py` next to the existing split/chain logic.

### 5b. Soniox endpoint tuning

Raise `stt_endpoint_delay_ms` from 1500 to 2500-3000. Soniox holds for more silence before emitting, absorbing the natural mid-sentence pauses that currently fragment the speech.

- **Pro:** Zero new code.
- **Pro:** Directly addresses the segmentation problem at its source.
- **Con:** Adds ~1 s of latency on every utterance (not just fragments), so the `video_delay` budget must rise by the same amount. Currently 16 s; would need to be 17-18 s.
- **Con:** Long monologues may still fragment because of `max_stt_duration` force-splits.
- **Effort:** Trivial. One yaml line. Worth measuring before considering anything heavier.

### 5c. Multimodal audio-to-audio translator (e.g. Gemini Live)

Replace the STT → translate → TTS chain with a single audio-in, audio-out model that sees acoustic continuity. The leaked Gemini Live model `gemini-3.1-flash-lite-live-translate` is a candidate; OpenAI Realtime and similar offerings are also in this category.

- **Pro:** Acoustic continuity is preserved — the model can recognise that `"…pass didn't work out"` is one continuous utterance regardless of where pauses fall.
- **Pro:** Potentially large latency win (1-3 s) from collapsing three stages into one.
- **Con:** Loses pre-TTS guard ability — by the time you see the translated text the audio is already streaming. Cannot drop a bad translation before it plays.
- **Con:** Loses per-language voice control unless the model exposes it.
- **Con:** Roster-based name correction and per-match `corrections.py` rewrites cannot be applied pre-output.
- **Con:** Currently early-access / closed beta, no SDK support, no production track record on football commentary. Treat as a research bet, not a near-term swap.
- **Effort:** Significant. Parallel evaluation pipeline first; full migration would be a multi-week project.

### Recommended ordering

1. Try **5b** (endpoint tuning) first — it is one yaml line and a 7-minute eval run. If raising `stt_endpoint_delay_ms` to 2500 ms and `video_delay` to 17.5 s recovers natural sentence boundaries on the canonical fragments without harming `fully_played_pct`, that is the cheapest fix.
2. If 5b helps but not enough, try **5a** as a complement — same-speaker fragment merging is bounded and reversible.
3. **5c** is a separate research track. Stand up a parallel side-channel evaluation against a real match clip; do not migrate production behaviour before audible quality is measured by native-speaker review on all five target languages.

## Phase 4 — Optional Language Detection

Add language detection only if logs show recurring English bleed-through or wrong-language output after Phase 1.

Do not add this dependency preemptively; length/refusal guards are cheaper and directly cover the observed failure.

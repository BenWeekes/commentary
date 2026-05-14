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

Implemented behind `translation_context_enabled`.

Add previous English and previous target-language translation as optional context per language. Context is selected by source audio time rather than translation completion time so parallel preparation cannot feed future utterances into the prompt.

Use behind a config flag because it may improve fragments such as `Work out.` or `Remaining.`, but it can also cause context carry-over and translation drift.

Evaluate separately from Phase 1 so any quality changes are attributable.

## Phase 4 — Optional Language Detection

Add language detection only if logs show recurring English bleed-through or wrong-language output after Phase 1.

Do not add this dependency preemptively; length/refusal guards are cheaper and directly cover the observed failure.

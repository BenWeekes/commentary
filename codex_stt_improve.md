# Commentary Quality Improvements

Prioritized suggestions from the `m05_uni_md33` run review.

## Precondition

Quality should only be judged after the live playback anchor fix is present:
`2daf642 fix(live): anchor stt playback to audio feed`.

Before that fix, translated audio could play around 7 seconds after the matching video frame. With that sync error, even good STT and translations can feel wrong.

## Rollout Order

Ship these as small, independently revertable changes:

1. Prompt-only changes.
2. STT threshold changes.
3. Diarization and speaker tags.
4. Additional telemetry and Soniox A/B testing.

## Recommendations

1. Loosen STT endpointing.
   - Move toward `endpointing=500ms`, `utterance_end_ms=1500ms`, and `MAX_STT_DURATION=8s`.
   - This is the biggest quality lever because many bad translations are faithful translations of broken fragments such as `Fancy a`, `been able to`, and `Football here in`.
   - Mirror threshold changes in `test_live_pipeline.py`.

2. Enable Deepgram diarization behind a feature flag.
   - Add `diarize=true` to the Deepgram live connection only when the flag is enabled.
   - Extract speaker labels from word-level results.
   - Log `speaker` on STT and translated rows.
   - Do not merge across speaker changes.
   - Use speaker changes as natural turn boundaries, especially commentator vs expert.
   - Keep this feature-flagged because Deepgram speaker IDs can flip during a call and need A/B validation.
   - Re-verify that keyterms plus `diarize=true` plus `smart_format=true` does not cause Deepgram connection failures.

3. Give the translator speaker context.
   - Pass recent context as speaker-tagged text, for example `[Speaker 1] ...`, `[Speaker 2] ...`.
   - Tell the translator that speaker tags are context only and must not be translated.
   - Treat a speaker change as a fresh turn, not as a continuation that needs stitching.

4. Add conservative STT-mishear correction to the translation prompts.
   - Apply this to both `TRANSLATE_SYSTEM` and `TRANSLATE_SYSTEM_WITH_ROSTER`.
   - Tell the translator the English source is from STT and may contain plausible mishearings.
   - Allow single-word substitution only when the word is clearly inconsistent with football commentary and one obvious replacement preserves the sentence structure.
   - Never use this rule to correct names. Player, team, venue, manager, and referee names must be corrected only via the roster/keyterm rule.
   - Do not change subject, action, score state, or tactical meaning.
   - Log every correction as `stt_correction_applied`, for example `voting->playing`, so it can be audited.
   - Example allowed: `Plenty of Austrians voting here` can be translated as if it said `playing here`.
   - Example not allowed: changing `Skhiri` to `Khedira` via this rule, because that is a name correction.
   - Do not guess when intent is unclear, for example `He'll learn Ilic`.

5. Improve fragment handling in the translation prompts.
   - Apply this to both `TRANSLATE_SYSTEM` and `TRANSLATE_SYSTEM_WITH_ROSTER`.
   - If the source ends with a preposition, conjunction, article, or comma, translate it as a fragment.
   - Do not complete the thought.
   - Do not add explanatory padding.
   - This reduces weird target-language completions when STT still splits mid-sentence.

6. Add targeted output sanitization.
   - First trace examples such as German `schau mal###` to confirm whether the artifact came from the model output or from text fed into the prompt.
   - Strip only known patterns such as `###`, leading `>`, stray speaker tags, and accidental markdown fences.
   - Do not add broad generic special-character stripping; it can damage valid names, punctuation, and non-English text.

7. Add telemetry for quality review.
   - Log `speaker`, `is_fragment`, `fragment_reason`, and `stt_correction_applied`.
   - Count short utterances and fragment-like utterances in the status UI.
   - This makes customer complaints easier to tie to STT segmentation, STT recognition errors, or translation behavior.

8. A/B test Soniox, do not switch immediately.
   - Deepgram already supports diarization, so start there.
   - Run Soniox in a test path against demo/live PCM and compare speaker labeling, player names, latency, and fragment rate.
   - Replace Deepgram only if evidence shows better live football STT.

9. Add a live-demo sync smoke test.
   - Run the `demo_srt_direct` flow end to end.
   - Assert translated utterance rows include `intended_skew_ms` and that absolute skew stays under 50 ms.
   - This would have caught the historical 7 second live sync bug before a real match.

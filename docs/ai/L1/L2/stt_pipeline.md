# L2 — STT Pipeline

> **When to Read This:** You are modifying the Deepgram integration, adding or editing STT corrections, changing the forced split logic, debugging translation latency, or working on the multi-language fan-out.

## Overview

The STT pipeline streams live game audio through the selected provider, corrects football phrases and player/team names, translates to the viewer's language, and schedules TTS playback at the original commentary timing.

## Pipeline Stages

```
Audio ──▶ ffmpeg ──▶ PCM ──▶ STT provider ──▶ corrections ──▶ tts.speak(play_at=...)
          (16kHz mono)       (Nova-3)      (str.replace)   → translate + TTS in worker
```

## Single-Language vs Multi-Language Mode

The STT pipeline has two entry points sharing a common core (`_run_stt_core()`):

### run_stt_pipeline() (dev mode)

Used by `live_match.py`. One Deepgram connection per viewer session. The `emit_fn` calls `tts.speak()` directly on the session's TTSEngine with JIT translation.

### run_stt_pipeline_multi() (server mode)

Used by `server/match_worker.py`. One STT connection per match, shared across all languages. The `emit_fn` calls an `on_utterance` callback that fans out to all language pipelines.

```
_run_stt_core()
    │
    └─ emit_fn(corrected_text, audio_start, audio_end)
         │
         ├─ [dev mode]    tts.speak(text, play_at=..., translate_fn=...)
         │                (single language, single TTSEngine)
         │
         └─ [server mode] on_utterance(text, audio_start, audio_end, play_at)
                          │
                          └─ MatchWorker._on_utterance()
                               ├─ es: tts.speak(text, play_at=lang_play_at, translate_fn=...)
                               ├─ pt: tts.speak(text, play_at=lang_play_at, translate_fn=...)
                               ├─ fr: tts.speak(text, play_at=lang_play_at, translate_fn=...)
                               ├─ tr: tts.speak(text, play_at=lang_play_at, translate_fn=...)
                               └─ de: tts.speak(text, play_at=lang_play_at, translate_fn=...)
```

The `_on_utterance` fan-out pattern sends the same corrected English text to all language pipelines simultaneously. Each pipeline translates independently (with its own `translate_fn`), uses its own TTSEngine, and plays through its own Go publisher. Per-language `video_start` values allow accurate `play_at` timing even if publishers started at slightly different times.

In server mode, `_on_utterance` also feeds the structured match log:

- the English STT utterance is appended to `recent_transcript`
- the same utterance is written to `match_data/{match_id}/runs/{timestamp}/stt.jsonl`
- each language pipeline later records its own translated playback outcome in `{lang}.jsonl`

## play_at Scheduling

The Go publisher delays video by `--video-delay` seconds while the STT pipeline processes audio immediately. This gives the pipeline a head start. Each STT result includes `audio_start` — when the commentator spoke in the original source media timeline.

In live server mode, STT and video use the same source media origin exposed by the SRT/direct publisher:

```python
play_at = source_media_start_wall + audio_start + video_delay + provider_offset
```

`provider_offset` is configured globally or per match via `stt_playback_offsets_ms` to compensate provider word/onset semantics, not network latency. Current latency-marker values are `soniox: 700ms` and `deepgram_nova3: 830ms`.

Publisher "video delay complete" messages are diagnostics; the live language clocks are not retimed after startup because that can create audible drift. The per-language logs include `intended_skew_ms`, which compares the actual scheduled `play_at` against the provider-normalized formula above and should stay near 0ms.

The TTS worker holds the audio until `play_at`, then plays at the exact scheduled time. If translate+TTS takes too long and `play_at` has already passed, the utterance is dropped.

### Server mode timing

In demo server mode, the worker computes one authoritative `target_start` and gives each language pipeline the same schedule basis. In live server mode, the STT callback passes the already-computed live `play_at` through to each language; `MatchWorker` applies only the provider offset and must not recompute from `pipe.video_start + audio_start`.

## Provider Configuration

Live mode supports Deepgram Nova-3 and Soniox realtime. `stt_provider` selects the provider per match or per manual start request. `stt_endpoint_delay_ms` controls Soniox endpointing; `max_stt_duration` is enforced on both providers to prevent turns that overrun the video-delay budget.

Deepgram Nova-3 configuration:

```python
model="nova-3", language="en", encoding="linear16", sample_rate=16000,
punctuate="true", smart_format="true", interim_results="true",
endpointing="200", utterance_end_ms="1000", keyterm=TERMS_LIST
```

- `endpointing=500`: Deepgram fires `speech_final` after 500ms silence in the current live tuning
- `utterance_end_ms=1000`: Minimum allowed by Deepgram API
- `is_final=True` results are processed; interims are monitored for forced splitting
- `keyterm`: player/team names for recognition boost. For live matches, keyterms are generated dynamically from the Sportradar lineups API and stored under `match_data/{match_id}/keyterms.txt` (full names, surnames, team names, venue, referees). Static `TERMS_LIST` remains a fallback.

Soniox realtime uses `model=stt-rt-v4`, keyterm context from the same roster-derived terms, speaker-aware tokens when available, and client-side turn emission at `stt_endpoint_delay_ms` or `max_stt_duration`. The Soniox `max_stt_duration` path uses a rolling safe split point: latest sentence boundary first, then speaker change, strong pause, or clause boundary. If no safe boundary exists, it waits up to 1s past the soft threshold before hard-emitting. This avoids splitting subword token pairs such as `candid` / `ates` when the completion arrives just after the nominal duration threshold.

## Latency Budget

With `--video-delay N` (live config currently uses 14s):

```
Budget per utterance ≈ N - utterance_duration - ~1.5s (translate + TTS fetch)

Example: 3s utterance, 7s delay → 7 - 3 - 1.5 = 2.5s margin (comfortable)
Example: 5s utterance, 7s delay → 7 - 5 - 1.5 = 0.5s margin (tight)
Example: 7s utterance, 7s delay → 7 - 7 - 1.5 = -1.5s margin (drops likely)
```

The TTS worker uses **lookahead**: while the current utterance plays, the next one is already being translated and TTS'd in parallel. It also locally speed-fits generated PCM with ffmpeg `atempo` when the current clip would overrun the next STT play time. Speed fitting targets the next STT item only, not future SR events.

## Forced Split (Long Utterance Protection)

Stadium crowd noise prevents Deepgram's VAD from detecting commentary pauses — audio levels only drop from -20 dB (speech) to -37 dB (crowd), well above Deepgram's silence threshold. This causes occasional mega-batches (6-10s) that exhaust the video delay budget.

The pipeline monitors interim results and force-splits when duration exceeds `--max-stt-duration`:

```
Normal:   is_final(3.5s) → emit → is_final(4.2s) → emit
Forced:   interim(5.0s) → SPLIT emit → is_final(7.6s) → REMAINDER emit (from 5.0s onward)
```

- Split uses the interim transcript (slightly unstable but better than dropped)
- The subsequent `is_final` emits only the remainder portion with adjusted `play_at`
- One split per utterance — enough to bring 9.4s worst-case batches into budget
- At 10s delay with forced split: zero drops across 5 languages

## Correction System

Two approaches:

### Global football corrections

`GLOBAL_FOOTBALL_CORRECTIONS` in `lib/corrections.py` fixes high-confidence football misrecognitions such as contextual "Freak has been given" -> "Free kick has been given". These corrections are applied before translation for both Deepgram and Soniox.

### Roster-based correction

The live path then applies deterministic roster/keyterm name correction in `lib/translator.py` before fan-out. It removes commas inside known full names, fixes wrong first names before known surnames, and applies close capitalized-name matches. The player roster from Sportradar lineups API is also included in the GPT translation prompt (`TRANSLATE_SYSTEM_WITH_ROSTER`) so translations keep football names in context.

## Language Switching

In dev mode, language is read from a per-session file at translation time (not queue time), so language changes take effect on the next utterance.

In server mode, each language has its own pipeline and Agora channel. Viewers switch languages by changing channels in the viewer.

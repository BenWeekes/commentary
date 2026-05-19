# Voice-to-Voice Provider A/B Plan

## Goal

Add a voice-to-voice translation path to the existing live commentary demo so each target language can independently run either:

- `classic`: current STT -> translate -> TTS pipeline.
- `v2v_openai`: one OpenAI Realtime session for that target language.
- `v2v_gemini`: one Gemini Live session for that target language.
- `v2v_xai`: optional, after validating x.ai realtime voice behavior against this use case.

The useful demo shape is per-language mode selection within one match. That lets the same source PCM, same video delay, same Agora viewer surface, and same recording/eval pipeline compare providers directly.

## Existing Pieces To Keep

- SRT pull -> `publish_srt_to_agora.py` -> local PCM TCP fanout + cleaned H.264.
- Per-language Go `relay_publish` process publishing to a per-language Agora channel.
- Translated-channel video delay buffer, where frames are held by `video_delay`.
- `status.html`, `viewer_live.html`, recordings, and existing JSONL logs.
- `demo_srt_direct` loopback source for deterministic clip-based iteration.
- Classic STT pipeline for baseline/control languages.

## Main Design

The current audio path for translated commentary is:

```text
source PCM -> STT -> text translation -> TTS -> relay_publish.stdin -> Agora lang channel
```

For v2v languages it becomes:

```text
source PCM -> provider realtime session -> translated PCM -> paced writer -> relay_publish.stdin -> Agora lang channel
```

Each v2v language gets its own source PCM TCP consumer because realtime voice sessions are target-language-specific. Classic languages continue sharing one STT session.

## Config Shape

Extend match language entries from plain language names to objects with explicit mode and optional provider settings.

```yaml
- match_id: v2v_demo
  mode: live
  source:
    type: demo_srt_direct
    demo_media_file: clips/bmg_fch_demo_5min/source.mp4
    demo_srt_port: 10082
  video_delay: 14.0
  stt_provider: soniox
  languages:
    - {lang: es, mode: classic}
    - {lang: fr, mode: v2v_openai, voice: alloy}
    - {lang: pt, mode: v2v_gemini, voice: Aoede}
    - {lang: de, mode: v2v_openai, voice: shimmer}
    - {lang: tr, mode: classic}
```

Backward compatibility: existing `languages: [es, fr, ...]` should parse as `mode: classic`.

## New Modules

Create `lib/v2v/` with a common adapter contract and one provider file per implementation.

```python
def run_v2v_pipeline_live(
    audio_pipe,
    output_audio_writer,
    on_transcript,
    stop_event,
    target_lang,
    video_delay,
    source_media_start_ref,
    voice_id=None,
    provider_options=None,
) -> int:
    """Run one provider voice-to-voice session for one target language."""
```

Provider modules:

- `lib/v2v/openai_realtime.py`
- `lib/v2v/gemini_live.py`
- `lib/v2v/xai_realtime.py` only after a small spike

Add `lib/v2v/base.py` for shared types and helper functions, such as PCM chunk sizing, transcript event normalization, reconnect policy, and provider metrics.

Adapter-wide contracts:

- Output audio handed to `PacedPipeWriter` must always be 16 kHz mono s16le PCM.
- Each provider adapter owns any required provider input/output resampling.
- Each provider adapter must be reconnect-aware from day 1: reconnect loops, session renewal hooks, and state reset should live in the adapter lifecycle, not be bolted on after the first demo.
- Language identifiers must go through a shared mapping helper. The app may use `fr`, while Gemini may need BCP-47 such as `fr-FR`, OpenAI may work best with ISO-639-1 such as `fr`, and prompts may need display names such as `French`.
- Transcript callbacks should emit normalized roles and timestamps even when provider event ordering is imperfect.

## Provider Notes

### OpenAI Realtime

Use the Realtime WebSocket for server-to-server audio. Stream source PCM via `input_audio_buffer.append`, listen for translated output audio deltas, and configure the session prompt to translate live football commentary into the target language.

Implementation notes:

- Confirm required input/output sample rates at adapter startup and resample where needed.
- Use `response.output_audio.delta` for output bytes.
- Use input transcription events such as `conversation.item.input_audio_transcription.delta` / `completed` for original transcript where enabled.
- Use output transcript events for translated transcript where available.
- Prefer server VAD at first; manual commit can be added later if we need stricter turn boundaries.
- Implement reconnect inside this adapter in Phase 4, even if fallback-to-classic stays deferred.

### Gemini Live

Use Gemini Live WebSocket / SDK with realtime audio input and audio output. Public docs describe raw 16-bit PCM input, 24 kHz audio output, optional input/output audio transcription config, and session limits/resumption. They also state generic native-audio models choose output language automatically rather than accepting an explicit output language code.

There may also be a closed/beta translate-specific Gemini model surface, e.g. `gemini-3.1-flash-lite-live-translate` with `streamingTranslationConfig.targetLanguageCode`. Do a half-day Gemini API spike before the full adapter to confirm which surface we have credentials/access for and whether the same setup message can support both generic Live and translate-specific Live.

Implementation notes:

- Send 16 kHz PCM source chunks with `audio/pcm;rate=16000`.
- Resample provider 24 kHz PCM output to the relay's 16 kHz mono s16le contract.
- Enable `inputAudioTranscription` and `outputAudioTranscription` in setup.
- Implement planned reconnect/session renewal because audio-only sessions and WebSocket connections have documented duration limits.
- Treat Gemini reconnect/session renewal as a core adapter requirement, not polish.

### x.ai Realtime Voice

x.ai now documents a realtime voice WebSocket at `wss://api.x.ai/v1/realtime`, with voice models such as `grok-voice-latest`, input audio buffer events, output audio deltas, input transcription completion, and output audio transcript deltas. It should be included as an optional adapter after OpenAI and Gemini are working.

The spike should answer:

- Can a system prompt reliably force one-way translation to a target language?
- Does it support 16 kHz PCM output directly, or do we need resampling from 24 kHz or another format?
- Are output transcript events timely enough for eval JSONL pairing?
- Are the target languages needed by the demo supported for voice output?

## Pacing And Audio Output

This is the critical shared component.

Today `TTSEngine._pipe_writer` paces buffered TTS audio into `relay_publish.stdin` in 10 ms chunks. V2V providers may return audio faster than realtime once generation starts, so every adapter must feed a paced writer instead of writing directly to the relay.

Extract a reusable `lib/paced_pipe_writer.py`:

- Accept PCM bytes from a producer thread/coroutine.
- Normalize to 16 kHz mono s16le.
- Hold first playable audio until `source_media_start_ref + source_audio_offset + video_delay`.
- Write exactly one 10 ms chunk every 10 ms to the output pipe.
- Emit telemetry for first-audio latency, buffered audio duration, underruns, interrupted/dropped status, and played duration.
- Shut down cleanly when the relay pipe closes or `stop_event` is set.

Classic `TTSEngine` should use this shared writer after extraction, with no behavior change. V2V adapters should push provider audio into a per-language instance of the same writer.

## Match Worker Dispatch

Update `server/config.py` to parse language entries as objects while preserving legacy strings.

Update `server/match_worker.py` live flow:

1. Resolve source and start one relay publisher per language, as today.
2. Build `_LangPipeline` with `mode`, `provider`, and `voice_id`.
3. Start classic `TTSEngine` only for classic languages.
4. Start one shared STT session only if any language is `classic`.
5. Start one v2v worker per v2v language, each with its own PCM TCP client.
6. Stop all workers through the existing match stop path.

The source PCM TCP fanout is the right mechanism for A/B: each v2v worker opens an additional client connection to the same local PCM listener, while the classic STT consumer remains unchanged.

## Transcript And Eval Logging

Keep one `{lang}.jsonl` file per target language. V2V rows should be compatible with existing eval tooling and add provider-specific fields.

```json
{
  "type": "utterance",
  "source": "v2v_openai",
  "status": "played",
  "audio_start": 12.3,
  "audio_end": 14.1,
  "original": "Free kick to Mainz",
  "translated": "Coup franc pour Mayence",
  "play_at": 1710000012.3,
  "play_started_at": 1710000012.32,
  "voice_id": "alloy",
  "v2v_first_audio_ms": 420,
  "v2v_total_audio_ms": 1850,
  "provider_session_id": "..."
}
```

Transcript pairing will be imperfect because v2v audio and transcript events can arrive independently. Normalize this in the adapter:

- Track provider turn IDs / item IDs / response IDs.
- Attach input transcript to the nearest output turn from the same provider response where possible.
- If pairing is uncertain, still log a row with `pairing_status: "estimated"` so eval can separate it.

## Status And Viewer

`viewer_live.html` should not need changes because language selection already maps to Agora language channels.

`status.html` and status APIs should expose mode per language:

- `classic`
- `v2v-openai`
- `v2v-gemini`
- `v2v-xai`

Add provider health fields once v2v is running:

- connection state
- reconnect count
- first audio latency
- buffered audio milliseconds
- last transcript age

## Known Tradeoffs

No pre-output guard for v2v in the first version. Classic can inspect translated text before TTS; v2v audio may already be streaming before the output transcript is complete. For an evaluation demo, accept this and log what happened. Add a 1-2 second audio veto buffer later only if moderation/guard behavior becomes a requirement.

No automatic fallback from v2v to classic in the first version. Reconnect and session renewal are required for live matches and belong in the first working provider adapters. Fallback is useful but adds dispatch and duplicate-session complexity, so defer fallback until a single v2v language is stable.

One match needs one uniform `video_delay`. V2V may have lower first-audio latency than classic; holding v2v output to the same delayed video feed intentionally throws away some v2v speed advantage. That is acceptable for an A/B experiment because it isolates provider quality on the same viewer timeline. Customer-facing production runs could use lower `video_delay` for all-v2v matches or split v2v languages into a separate lower-delay match.

Cost is not negligible. A two-hour match with four v2v languages is roughly `2 hours * 4 languages * 2 audio directions = 16 audio-hours` billed before retries, reconnect overlap, and provider minimums. Price this with current provider rate cards before running full-length matches.

## Implementation Order

| Phase | Scope | Output |
| --- | --- | --- |
| 1 | Extract paced writer from `TTSEngine` into `lib/paced_pipe_writer.py` and keep classic behavior unchanged | Shared timing primitive with tests |
| 2 | Add backward-compatible per-language config parsing, mode/status fields, and language-code mapping helpers | YAML can select `classic` vs `v2v_*` per language |
| 3 | Add v2v dispatcher in `server/match_worker.py` with reconnect-aware stub provider | Process topology and lifecycle work before provider APIs |
| 4 | Implement OpenAI Realtime adapter end-to-end for one language, including reconnect | First audible v2v channel that can survive transient disconnects |
| 5 | Write v2v transcript/telemetry rows into `{lang}.jsonl` | Eval parity with classic path |
| 6 | Gemini API spike, then Gemini Live adapter with session renewal | OpenAI vs Gemini A/B demo without later API-surface rewrite |
| 7 | Status UI provider health and fallback policy decisions | Operator visibility for longer demo runs |
| 8 | Spike and optionally implement x.ai adapter | Third provider if translation behavior validates |

Phase 1-4 is the smallest credible demo. Phase 6 is the point where the A/B comparison becomes real.

## Validation Plan

- Unit test `PacedPipeWriter` with a fake pipe and fake monotonic clock:
  - writes 10 ms chunks at expected cadence
  - honors initial `play_at`
  - handles burst input without faster-than-realtime output
  - exits on broken pipe
- Regression test classic `TTSEngine` telemetry shape after extraction.
- Config test for old and new `languages` YAML forms.
- Provider dry-run tests behind env flags:
  - `OPENAI_API_KEY`
  - `GEMINI_API_KEY`
  - `XAI_API_KEY`
- End-to-end demo with `demo_srt_direct` and a 5 minute clip:
  - one classic baseline language
  - one OpenAI language
  - one Gemini language
  - verify all selected languages appear in `status.html`
  - verify `viewer_live.html?match=v2v_demo&lang=fr` plays translated audio
  - verify `{lang}.jsonl` has played rows with provider metrics

## Effort Estimate

| Phase | Estimate |
| --- | ---: |
| Pacing extraction and tests | 0.5-1.0 day |
| Per-language config and dispatch | 1.0 day |
| OpenAI adapter with reconnect | 2.0 days |
| Transcript/eval logging | 0.5 day |
| Gemini spike + adapter | 1.5-2.0 days |
| Status polish and fallback decision | 0.5-1.0 day |
| x.ai spike/adapter | 0.5-1.0 day |

Expected total for OpenAI + Gemini A/B demo: 7-8 days with realistic WebSocket, VAD, event ordering, reconnect, and audio framing risk.

Expected total including x.ai if it validates cleanly: 8-9 days.

## Open Questions

- Per-language mode is assumed. Per-match mode would be simpler but weaker for direct A/B.
- Pick one demo source: shorter `bmg_fch_demo_5min/source.mp4` for fast iteration, longer `m05_uni_eval_25min` for provider quality evaluation.
- Confirm desired voice pinning strategy: per-language YAML voice is best for reproducibility.
- Confirm Gemini surface: generic Live prompt-driven translation vs translate-specific Live model with explicit `streamingTranslationConfig.targetLanguageCode`.
- Decide whether to keep v2v provider output fully unguarded for all demo runs, or add an optional delayed-veto mode later.

## Docs Checked

- OpenAI Realtime conversations: https://developers.openai.com/api/docs/guides/realtime-conversations
- OpenAI Realtime API reference: https://developers.openai.com/api/reference/resources/realtime
- Gemini Live API guide: https://ai.google.dev/gemini-api/docs/live
- Gemini Live WebSocket reference: https://ai.google.dev/api/live
- x.ai realtime voice docs: https://docs.x.ai/developers/rest-api-reference/inference/voice

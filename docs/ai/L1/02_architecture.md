# L1 — Architecture

> Server mode, demo/live match modes, timing model, and the multi-language pipeline.

## What We're Building

A live commentary translation service for live football matches. The primary commentary source is **STT from the live commentator's audio**, transcribed via the configured live STT provider. SR (Sportradar) AI-generated text is a secondary, lower-priority gap-filler — it plays only when the commentator is silent.

Both sources are translated and spoken via TTS, synced to delayed video so the viewer hears translated commentary at the exact moment the original was spoken.

Current implementation note:

- **Demo mode** has both STT and SR gap-fill today.
- **Live mode** currently uses STT only. Sportradar in live mode is used for fixture metadata refresh (kickoff, roster, keyterms), not for real-time commentary injection yet.

## Server Mode

The production server (`server/`) manages multiple matches simultaneously. Each match runs one STT pipeline that fans out to N language pipelines, each with its own Go publisher and Agora channel.

```
matches.yaml
    │
    ▼
┌───────────────────┐     ┌────────────────────────┐
│ server/main.py    │────▶│ Orchestrator            │
│ (entry point)     │     │  ├─ MatchWorker(bmg_fch)│
└───────────────────┘     │  │   ├─ STT (1×)        │
                          │  │   ├─ es pipeline      │
    ┌─────────────┐       │  │   ├─ pt pipeline      │
    │ status_api  │◀──────│  │   ├─ fr pipeline      │
    │ (HTTP :8080)│       │  │   ├─ tr pipeline      │
    └─────────────┘       │  │   └─ de pipeline      │
                          │  └─ MatchWorker(...)      │
                          └────────────────────────────┘
```

**Key design**: one STT connection per match, shared across all languages. Live mode can use Deepgram Nova-3 or Soniox realtime depending on `stt_provider`; the `_on_utterance` callback fans each corrected utterance to all language pipelines (translate → TTS → Go publisher → Agora channel).

### Server vs dev mode

| Aspect | Server mode (`server/main.py`) | Dev mode (`live_match.py`) |
|---|---|---|
| Config | YAML file (`matches.yaml`) | CLI args |
| Matches | Multiple simultaneous | Single |
| Languages | N per match (shared STT) | One per session (separate STT) |
| Viewer | `viewer_live.html` | `viewer.html` |
| Port | 8080 (default) | 8090 |
| Session model | Per-language channels, shared match | Per-viewer sessions, isolated pipelines |

Dev mode (`live_match.py`) still works and is useful for single-match development and testing.

## Demo Match Mode

Demo mode uses pre-recorded audio/video files with a single STT pipeline fanning out to multiple languages. This is the current production workflow via the server.

```
┌──────────────┐    ┌──────────┐    ┌──────────────────┐
│ Pre-recorded │──▶ │ Deepgram │──▶ │ Correct names     │
│ audio (file) │    │ Nova-3   │    │ apply_corrections │
└──────────────┘    └──────────┘    └────────┬──────────┘
                                             │
                                    _on_utterance (fan-out)
                                             │
                    ┌────────────────┬────────┼────────┬────────────────┐
                    ▼                ▼        ▼        ▼                ▼
              ┌──────────┐    ┌──────────┐         ┌──────────┐  ┌──────────┐
              │ Translate │    │ Translate │   ...   │ Translate │  │ Translate │
              │ → TTS es  │    │ → TTS pt  │         │ → TTS tr  │  │ → TTS de  │
              └─────┬─────┘    └─────┬─────┘         └─────┬─────┘  └─────┬─────┘
                    ▼                ▼                      ▼              ▼
              Go pub → Agora   Go pub → Agora        Go pub → Agora Go pub → Agora
              bmg_fch-es       bmg_fch-pt             bmg_fch-tr    bmg_fch-de
```

Each language pipeline has its own Go publisher that publishes delayed video + translated TTS audio on a dedicated Agora channel (`{match_id}-{lang}`).

SR events run in parallel: the SRPrefetcher pre-translates and pre-TTS's each event per language, scheduled to play at exact match time.

## Live Match Mode

Live matches support three source modes:

- `source.type = agora`
  Uses an existing Agora source channel with explicit source UIDs.
- `source.type = srt`
  Pulls one remote SRT feed, republishes it into an internal Agora channel, then runs the normal live worker from there.
- `source.type = srt_direct`
  Pulls one remote SRT feed once with FFmpeg/libav because the media-gateway path is not reliable for this feed. It exposes local commentary PCM, optional local atmosphere PCM, and cleaned H.264 fanout for the translated path, and separately republishes a buffered original channel into Agora for viewer "original".

Agora-backed live matches use a source channel where the broadcaster publishes three UIDs:

| Source UID | Content |
|---|---|
| 73 | Live video |
| 74 | Stadium atmosphere audio |
| 75 | Live commentary audio |

### Architecture

```
Source Agora Channel
  UID 73 (video) ──────────────────┐
  UID 74 (atmosphere) ─────────────┤
  UID 75 (commentary) ─────┐       │
                            │       │
                            ▼       ▼
                    subscribe_audio.go    relay_publish.go (per lang)
                    (subscribes UID 75)   (subscribes UIDs 73 + 74)
                            │                     │
                    PCM stdout → Python    Delay buffer (video_delay seconds)
                            │                     │
                    STT provider → Correct        │
                            │                     │
                   _on_utterance fan-out           │
                            │                     │
               ┌────────┬───┴───┬────────┐        │
               ▼        ▼       ▼        ▼        │
          Translate  Translate  ...   Translate    │
          → TTS es   → TTS pt        → TTS de     │
               │        │               │         │
               ▼        ▼               ▼         ▼
         Output channel per language:
         delayed video (73) + mixed audio (delayed atmos + TTS) — no UID 75
```

Components:

- `server/live_source.py` — resolves `agora`, `srt`, or `srt_direct` and owns any required source-side processes
- `subscribe_audio.go` (`go-audio-video-publisher/cmd/subscribe_audio/`) — subscribes to the resolved source channel, writes commentary/program PCM to stdout. Python STT reads from this process's stdout via `pcm_stream_from_pipe()`.
- `lib/stt_pipeline.py` / `lib/soniox_stt_pipeline.py` — live STT provider implementations. Deepgram Nova-3 remains supported; Soniox realtime (`stt-rt-v4`) is the current preferred live-demo path because it produced better football STT on the Mainz/Union evaluation clip.
- `relay_publish.go` (`go-audio-video-publisher/cmd/relay_publish/`) — subscribes to resolved source video and optional atmosphere, holds frames in a delay buffer for `video_delay` seconds, then publishes to the output channel. Audio output is delayed source atmosphere mixed with translated TTS from stdin when source atmosphere is enabled.
- One `relay_publish` process per language.

For `source.type = srt`:

- one combined program feed is published on `source.publish_uid`
- STT reads that program feed
- source atmosphere is disabled explicitly in `relay_publish`
- output channels carry delayed video + translated TTS only
- when using direct encoded H.264 from SRT, the publisher first repacketizes each access unit: drop `AUD`/filler NALs, preserve SPS/PPS for keyframes, and emit clean IDR/P-slice AUs before `PushVideoEncodedData`

For `source.type = srt_direct`:

- the source is pulled once by a single Go process
- that process can select separate SRT audio streams for commentary/program audio and stadium atmosphere (`source.audio_stream_index`, `source.atmosphere_audio_stream_index`)
- commentary/program audio is decoded to local PCM for Python STT immediately
- atmosphere audio is decoded to a separate local PCM fanout for translated relays when configured
- the same process converts H.264 to Annex B if needed, parses complete access units, drops `AUD`/filler NALs, preserves SPS/PPS for keyframes, and exposes cleaned H.264 over a local TCP fanout for per-language `relay_publish`
- the viewer-facing original channel is still published into `source.original_channel` with `source.original_buffer_seconds` of source-side buffering; original audio is the selected commentary plus atmosphere mix
- translated relays use the full configured `video_delay`; the original-channel buffer is only for original viewing jitter smoothing
- translated outputs are delayed video plus mixed audio: delayed atmosphere from the local fanout and translated TTS

**Delay buffering** is the core design constraint: video and atmosphere are held for `video_delay` seconds to give the STT → translate → TTS pipeline time to process. The viewer sees delayed video with translated audio arriving in sync.

### Current live-mode limitation

- Live mode does **not** currently attach an `SRPrefetcher` or poll real-time Sportradar commentary endpoints.
- The live worker uses Sportradar only for pre-match / in-between refresh of lineup-derived roster text, keyterms, and kickoff metadata.

## SR Schedule Monitor

Live auto-managed matches are now handled by `server/scheduler.py`. The scheduler refreshes Sportradar fixture metadata, tracks kickoff countdown, and auto-starts live matches when they enter their configured `prestart_seconds` window before kickoff.

## Timing Model

```
The Go publisher/relay delays video by `--video-delay` seconds. Current live configs use 14s.
The STT audio feed starts immediately, giving translations a head start.

For live STT utterances:
  play_at = source_media_start_wall + audio_start + video_delay + provider_offset

  source_media_start_wall = live source media origin captured by the SRT/direct publisher
  audio_start = when the commentator spoke, in provider audio time
  video_delay = configured delay used by the relay/video path
  provider_offset = configured per-STT-provider alignment offset, e.g. Soniox 700ms,
                    Deepgram Nova-3 830ms on the latency marker clip

  This keeps translated audio aligned to the corresponding delayed video frame even if
  SRT source startup and relay startup do not happen exactly video_delay seconds apart.

For SR events:
  play_at = match_time_start + event_offset
  Prefetched — TTS is ready seconds before play_at. Always ±0ms.

Rule: play at exact play_at time, or drop the utterance.
```

In the current codebase, the SR-event branch above applies to demo/event-file mode. Live mode still uses the STT branch only.

## Startup Sequence

1. Go publisher connects to Agora, starts reading audio from stdin immediately
2. The worker computes a shared `target_start` for all translated output channels
3. STT pipeline starts — audio feed begins, the selected STT provider processes in real-time
4. Go publisher waits until `target_start` / finishes its delay buffer
5. After delay, publisher starts sending video and confirms `video delay complete`
6. Translations from step 3 are already ready → play in sync with video

**Timing invariant**: STT utterances use the shared `target_start` as authoritative video start. This is not a fallback or best-effort guess; it is the primary synchronization mechanism that lets STT spend the full delay budget on transcription, translation, and TTS before first video.

**Why the target is valid**: the target is passed into the relay/publisher process as an absolute start time. Publishers may report their actual first-video time for diagnostics, but the language clocks are not retimed after startup because retiming live queues would create audible drift.

**Why `video delay complete` is still logged**: it proves the publisher reached the shared start and is useful for drift diagnostics. It is not used to move already scheduled STT playback.

### Server mode startup differences

In server mode, each language has its own Go publisher/relay, but the MatchWorker computes one shared `target_start` before STT begins:

1. Resolve the live source and any local SRT-direct fanout sockets
2. Compute shared `target_start = now + connection_margin + relay_delay`
3. Store that target in `video_start_ref` for STT scheduling
4. Start each per-language relay with `--start-at {target_start}`
5. Start STT immediately so STT, translation, and TTS work during the delay window
6. Treat publisher "video delay complete" timestamps as diagnostics only; do not retime language clocks after startup

## Translation Optimization

Three mechanisms reduce the translate+TTS latency that causes dropped utterances:

### Parallel preparation

When STT emits a burst, each per-language `TTSEngine` starts translate+TTS preparation immediately on a bounded `ThreadPoolExecutor(max_workers=2)`. Prepared audio is kept local to the task, then inserted into a heap keyed by `play_at`. Playback remains serial and ordered, but preparation no longer waits for earlier utterances to finish playback. This removes most avoidable `queue_wait_ms` from clustered STT turns while keeping ElevenLabs concurrency bounded.

This does not fix structurally late STT turns where the provider emits the utterance too close to, or after, its `play_at`; those still require shorter STT turns or a larger `video_delay`.

### OpenAI warmup

The first translation call per process can incur a cold-start penalty. In server mode, `MatchWorker` fires one throwaway "Kick off." translation per non-English language in parallel threads immediately after creating the OpenAI client, before real utterances arrive. This warms both the configured primary model and the fast fallback model, and applies to file-demo plus live/demo-SRT paths so startup cost is not paid by the first real utterance.

### STT turn sizing

Live mode must bound STT turn duration. If a provider emits a turn longer than `video_delay`, the play deadline can already be in the past before translation starts. Deepgram has interim force-splitting in `lib/stt_pipeline.py`; Soniox has client-side force emission in `lib/soniox_stt_pipeline.py` using the same `max_stt_duration` match setting. The current live-demo candidate is Soniox `stt-rt-v4`, `stt_endpoint_delay_ms=1500`, `max_stt_duration=6.5`, and `video_delay=14`.

### Name correction

Live correction has two deterministic layers. First, `GLOBAL_FOOTBALL_CORRECTIONS` fixes high-confidence football commentary STT errors such as contextual "Freak has been given" -> "Free kick has been given"; this is applied for both Deepgram and Soniox. Second, `correct_names_text_code()` fixes only high-confidence proper-name patterns such as comma insertion inside a known full name, wrong first name before a known surname, or a close capitalized name match. The older LLM name-correction helper remains available for offline experiments, but the live path avoids it because tail latency can exceed the available playback budget.

## Playback Rules

**STT is primary. SR is gap-fill only. Fresh STT may interrupt older STT.**

- **STT utterances**: translated + TTS'd as fast as possible. If ready before play_at, hold and play at exact time. If late, drop — the moment has passed. When a newer STT utterance has translated audio buffered and ready, it interrupts older queued or active STT so the output never drifts behind current commentary.
- **SR events**: lower-priority gap-fill. SR may only play when there is sufficient idle space around STT playback.
- **SR INTERRUPT** (e.g. GOAL): high priority within the SR queue, but does **not** preempt active STT. It waits for STT to finish, then plays in the next gap.
- **STT can interrupt SR**: if STT audio becomes ready while SR is playing, SR is interrupted immediately and STT takes over.
- **Preparation is bounded**: each language prepares at most two STT utterances in parallel; playback remains ordered by `play_at`, and stale items are replaced or dropped rather than allowed to drift.
- **Invariant**: STT interruption is allowed and counted separately from drops. `sr_cut_short_count` is expected and normal — it means the commentator was active.

## Atmosphere Audio

Optional stadium atmosphere (crowd noise) can be mixed under translated commentary:

```
atmosphere.wav ──▶ load_atmosphere() ──▶ raw PCM in memory
                                              │
                   _pipe_writer ◀──────────────┘
                        │
          ┌─────────────┼─────────────┐
          │             │             │
     TTS playing    SR playing    Idle (silence)
          │             │             │
     mix atmos      mix atmos     write atmos-only
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                   Go publisher stdin
```

- Mel-Band Roformer separated from original broadcast audio (16kHz mono S16LE WAV)
- Mixed at 0.5x volume to avoid clipping
- Per-sample S16LE addition with int16 clamping
- Position synced to video time on toggle (not from start of file)
- Toggled per-session via API: `/api/session/{id}/set-atmosphere?enabled=true`
- Viewer toggle: "Atmos" switch in top bar

## Original Audio Pass-Through

The "Original" toggle plays the source English commentary audio synced to video, bypassing translation entirely:

- Original audio PCM loaded from `--audio` at startup via `convert_to_pcm()` + `wave.open()`
- Position synced to video time when toggled on (`elapsed * 32000` aligned to 10ms)
- When enabled: atmosphere and language controls are disabled in the viewer
- `_pipe_writer` writes original chunks at 10ms rate, skipping TTS/SR playback
- STT + translate still runs in background; resumes naturally when toggled off
- API: `/api/session/{id}/set-original?enabled=true`
- Viewer toggle: "Original" switch in top bar

## Key Parameters

| Parameter | Default | Effect |
|---|---|---|
| `--video-delay` | 7.0s | Pipeline budget. Longer = more STT utterances survive |
| `--events-offset` | 0 | Match-time offset for events replay |
| `--lang` | es | Default translation language (dev mode) |
| `--atmosphere` | none | Path to atmosphere WAV (16kHz mono) |
| `endpointing` | 500ms | Deepgram VAD — shorter = faster turn detection |
| `utterance_end_ms` | 1000ms | Deepgram utterance boundary (minimum 1000ms) |

## Translation Models

Two models are benchmarked for translation:

| Model | Avg latency | Notes |
|---|---|---|
| `gpt-5.4` (reasoning=low) | current live default | Natural football phrasing while preserving meaning |
| `gpt-4o-mini` (temp=0.0) | older fallback | Fast and reliable fallback |
| `gpt-5.4-mini` (reasoning=low) | benchmarked fallback | Slightly more natural than `gpt-4o-mini`, occasional blank responses |

The live prompt tells the model to translate exactly what was said, avoid adding/removing/rewriting meaning, keep length and structure close to the original, fix roster names, and use natural football terminology in the target language.

The `translate_text()` function in `lib/translator.py` defaults to `gpt-5.4` with `reasoning_effort="low"` and accepts `model` and `reasoning_effort` parameters to switch. Server mode defaults to `gpt-5.4` via `server/config.py`, and `matches_live.yaml` also sets `translation_model: "gpt-5.4"`.

Live STT translation uses `translate_text_with_fallback()`: it starts the configured primary translation call and a fast `gpt-4o-mini` fallback in parallel. The primary wins if it finishes inside the grace window; after that, whichever finishes first is used. The losing request is allowed to finish in the background and may still be billed. Per-language JSONL rows record `translation_model_used` and `translation_fallback_reason` so fallback usage can be audited against drops.

Current live-demo evidence from `m05_uni_eval_demo` runs on 2026-05-13:

| Run | Change under test | Played | Dropped | Interrupted |
|---|---|---:|---:|---:|
| `20260513_092754` | fallback translation + bounded prepare baseline | 1811 | 97 | 120 |
| `20260513_182142` | late recheck for TTS gap fitting | 1849 | 86 | 99 |
| `20260513_190657` | same-speaker continuity chaining | 1918 | 64 | 112 |

The latest run has the best drop rate and highest played count. Interruptions are slightly higher than `20260513_182142`, mainly because more items play in busy regions. Remaining drops are usually caused by STT turns arriving too close to their scheduled `play_at`, leaving less than the roughly 1.8-2.2s needed for fallback translation plus TTS. This is the best current operating point for 14s video delay: Soniox `1500ms`, bounded two-way prepare per language, local TTS gap fitting, same-speaker continuity chaining, and primary translation with fast fallback.

Forced Soniox split continuations are marked explicitly and may be chained during playback. This only applies to turns split by our local `max_stt_duration` logic, not to ordinary adjacent utterances. The first split part stays anchored to source/video timing; later parts in the same split group can be advanced to follow the previous translated audio with a 30ms gap so artificial sentence splits do not create multi-second silences.

Adjacent normal STT turns can also be pulled closer together by a conservative source-timing rule: same speaker, source gap no more than 900ms, previous item completed normally, and a maximum 1500ms advance. This reduces artificial gaps introduced by endpointing without relying on phrase-specific grammar heuristics. In `20260513_190657`, this fired 21-28 times per language. The "Amiri ... going to be / Caught every day..." case joined to 100ms in EN and PT; ES/FR/TR did not chain that exact case because the needed advance exceeded the 1500ms cap.

## TTS Speed Fitting

ElevenLabs is requested with `speed=1.0`, `stability=1.0`, and `similarity_boost=1.0`. The engine does not ask ElevenLabs to speak faster by default; instead it fits generated PCM locally only when needed.

When the next STT play time is already known, `TTSEngine` fits the current generated PCM into the available gap before that next STT item, using ffmpeg `atempo` without changing pitch. The fit is capped to `1.3x` speed-up and `0.769x` slow-down; if the next STT play time is not known yet, the engine keeps the generated duration rather than fitting to provider word spans. Language logs record the applied speed as `speed` / `local_speed_factor`, plus `fit_from_ms`, `fit_to_ms`, `fit_deadline_ms`, `fit_cpu_ms`, and `fit_reason`.

## Roster-Aware Translation

When a Sportradar `sport_event_id` is available, the translation prompt includes the full player roster fetched from the lineups API. This allows GPT to fix STT name errors (e.g. "Jens Castro" → "Jens Castrop", "Heidenhain" → "Heidenheim") during translation without a static corrections list.

```
Sportradar lineups.json → roster string → TRANSLATE_SYSTEM_WITH_ROSTER prompt → GPT
```

The roster includes: team names, manager names, starting XI, substitutes, venue, referees.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — threading, buffer strategy, atmosphere mixing
- [STT Pipeline](L2/stt_pipeline.md) — Deepgram config, forced split, correction system, multi-lang fan-out

# L1 — Architecture

> Server mode, demo/live match modes, timing model, and the multi-language pipeline.

## What We're Building

A live commentary translation service for live football matches. The primary commentary source is **STT from the live commentator's audio**, transcribed via Deepgram. SR (Sportradar) AI-generated text is a secondary, lower-priority gap-filler — it plays only when the commentator is silent.

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

**Key design**: one Deepgram STT connection per match, shared across all languages. The `_on_utterance` callback fans each corrected utterance to all language pipelines (translate → TTS → Go publisher → Agora channel).

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
  Pulls one remote SRT feed once, republishes a buffered original channel into Agora using encoded video, then runs STT and translated relays from that same original channel.

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
                    Deepgram STT → Correct        │
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
- `relay_publish.go` (`go-audio-video-publisher/cmd/relay_publish/`) — subscribes to resolved source video and optional atmosphere, holds frames in a delay buffer for `video_delay` seconds, then publishes to the output channel. Audio output is delayed source atmosphere mixed with translated TTS from stdin when source atmosphere is enabled.
- One `relay_publish` process per language.

For `source.type = srt`:

- one combined program feed is published on `source.publish_uid`
- STT reads that program feed
- source atmosphere is disabled explicitly in `relay_publish`
- output channels carry delayed video + translated TTS only
- when using direct encoded H.264 from SRT, the publisher first repacketizes each access unit: drop `AUD`/filler NALs, preserve SPS/PPS for keyframes, and emit clean IDR/P-slice AUs before `PushVideoEncodedData`

For `source.type = srt_direct`:

- the source is pulled once and published into `source.original_channel` using encoded H.264 with `source.original_buffer_seconds` of source-side buffering
- STT subscribes to that buffered original channel
- translated relays also subscribe to that same buffered original channel
- downstream relay delay is reduced by `source.original_buffer_seconds` so total end-to-end delay still matches `video_delay`
- there is still no separate source-atmosphere bed; outputs are delayed video + translated TTS only

**Delay buffering** is the core design constraint: video and atmosphere are held for `video_delay` seconds to give the STT → translate → TTS pipeline time to process. The viewer sees delayed video with translated audio arriving in sync.

### Current live-mode limitation

- Live mode does **not** currently attach an `SRPrefetcher` or poll real-time Sportradar commentary endpoints.
- The live worker uses Sportradar only for pre-match / in-between refresh of lineup-derived roster text, keyterms, and kickoff metadata.

## SR Schedule Monitor

Live auto-managed matches are now handled by `server/scheduler.py`. The scheduler refreshes Sportradar fixture metadata, tracks kickoff countdown, and auto-starts live matches when they enter their configured `prestart_seconds` window before kickoff.

## Timing Model

```
The Go publisher delays video by --video-delay seconds (default 7s).
The STT audio feed starts immediately, giving translations a head start.

For STT utterances:
  play_at = video_start + audio_start

  video_start = wall time when Go publisher finishes the delay and sends first frame
  audio_start = when the commentator spoke (from Deepgram)

  Since the audio feed started video_delay seconds before video_start,
  translations are typically ready ~1-2s before play_at.

For SR events:
  play_at = match_time_start + event_offset
  Prefetched — TTS is ready seconds before play_at. Always ±0ms.

Rule: play at exact play_at time, or drop the utterance.
```

In the current codebase, the SR-event branch above applies to demo/event-file mode. Live mode still uses the STT branch only.

## Startup Sequence

1. Go publisher connects to Agora, starts reading audio from stdin immediately
2. After audio-ready is confirmed, `video_start` is estimated as `time.time() + video_delay`
3. STT pipeline starts — audio feed begins, Deepgram processes in real-time
4. Go publisher sleeps `video_delay` seconds (video frames held back)
5. After delay, publisher starts sending video → `video_start` is updated to actual time
6. Translations from step 3 are already ready → play in sync with video

**Timing invariant**: STT utterances scheduled before step 5 intentionally use the estimated `video_start`. This is not a fallback or best-effort guess; it is the primary synchronization mechanism that lets STT spend the full `video_delay` budget on transcription, translation, and TTS before first video.

**Why the estimate is valid**: the estimate is taken only after the Go publisher reports audio-ready, and from that point the publisher advances to first video frame by a local deterministic `time.Sleep(video_delay)`. There is no extra network-dependent stage between the estimate and the delayed video start, so `time.time() + video_delay` on the Python side and `time.Sleep(video_delay)` on the Go side converge to within a few milliseconds on the same machine.

**Why `video_start` is updated later**: once the publisher confirms video start, the actual timestamp is stored for log accuracy and for all post-start timing. Early STT utterances are not expected to be materially rescheduled by this update; they should already be aligned by design.

### Server mode startup differences

In server mode, each language has its own Go publisher with its own `video_start`. The MatchWorker:

1. Starts all N Go publishers in sequence, waits for audio-ready on each
2. Sets provisional `video_start_ref` for STT
3. Starts STT thread immediately (processes during video delay)
4. Waits for all publishers to report "video delay complete"
5. Computes mean `video_start` across all languages; logs warning if spread >500ms
6. Updates `video_start_ref` to actual mean; per-language pipelines use their own `video_start` for `play_at`

## Translation Optimization

Two mechanisms reduce the translate+TTS latency that causes dropped utterances:

### Pre-translation

When multiple items are queued in a TTSEngine, a `ThreadPoolExecutor(max_workers=2)` translates queued items in parallel ahead of the TTS worker thread. When the worker reaches an item, it checks the pre-translation cache first — on a hit, only TTS is needed (~0.8s instead of ~3s total). Typical cache hit rate is 100% under load. See [TTSEngine Internals](L2/tts_engine.md) for details.

### OpenAI warmup

The first translation call per process to `gpt-5.4-mini` incurs a ~15s cold-start penalty. In server mode, `MatchWorker._run_demo()` fires one throwaway "Kick off." translation per language in parallel threads immediately after creating the OpenAI client, before real utterances arrive. This absorbs the cold-start before the pipeline is timing-sensitive.

## Playback Rules

**STT is primary. SR is gap-fill only. STT is never interrupted.**

- **STT utterances**: translated + TTS'd as fast as possible. If ready before play_at, hold and play at exact time. If late, drop — the moment has passed. STT always plays to completion — nothing can interrupt active STT playback.
- **SR events**: lower-priority gap-fill. SR may only play when there is sufficient idle space around STT playback.
- **SR INTERRUPT** (e.g. GOAL): high priority within the SR queue, but does **not** preempt active STT. It waits for STT to finish, then plays in the next gap.
- **STT can interrupt SR**: if STT audio becomes ready while SR is playing, SR is interrupted immediately and STT takes over.
- **Queue stays at 0-1**: when a new STT utterance arrives with play_at, any stale queued item is replaced.
- **Invariant**: `stt_cut_short_count` should always be 0. `sr_cut_short_count` is expected and normal — it means the commentator was active.

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
| `endpointing` | 200ms | Deepgram VAD — shorter = faster turn detection |
| `utterance_end_ms` | 1000ms | Deepgram utterance boundary (minimum 1000ms) |

## Translation Models

Two models are benchmarked for translation:

| Model | Avg latency | Notes |
|---|---|---|
| `gpt-4o-mini` (temp=0.0) | ~0.95s/call | Fastest, most reliable, no blank responses |
| `gpt-5.4-mini` (reasoning=low) | ~1.67s/call | Slightly more natural phrasing, occasional blank responses |

Both produce faithful translations. `gpt-5.4-mini` with `reasoning=medium` is not recommended — it rewrites commentary and returns blank responses.

The `translate_text()` function in `lib/translator.py` defaults to `gpt-5.4-mini` with `reasoning_effort="medium"` but accepts `model` and `reasoning_effort` parameters to switch. Server mode defaults to `gpt-4o-mini` via the `translation_model` config field.

## Roster-Aware Translation

When a Sportradar `sport_event_id` is available, the translation prompt includes the full player roster fetched from the lineups API. This allows GPT to fix STT name errors (e.g. "Jens Castro" → "Jens Castrop", "Heidenhain" → "Heidenheim") during translation without a static corrections list.

```
Sportradar lineups.json → roster string → TRANSLATE_SYSTEM_WITH_ROSTER prompt → GPT
```

The roster includes: team names, manager names, starting XI, substitutes, venue, referees.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — threading, buffer strategy, atmosphere mixing
- [STT Pipeline](L2/stt_pipeline.md) — Deepgram config, forced split, correction system, multi-lang fan-out

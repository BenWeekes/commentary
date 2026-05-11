# L1 — Interfaces

> Production server API, Agora channel contracts, PCM audio format, dev-mode API, and live mode contracts.

## Production Server HTTP API

Served by `StatusHandler` in `server/status_api.py` on port 8080 (configurable via `control_port` in YAML).

### Match management

| Endpoint | Method | Response | Purpose |
|---|---|---|---|
| `/api/status/overview` | GET | `[{match_id, display_name, mode, enabled, auto_manage, scheduler_state, kickoff_utc, countdown_seconds, last_refresh_at, last_error, worker_state, ...}]` | Scheduler-oriented overview for all configured matches |
| `/api/matches` | GET | `{match_id: {match_id, display_name, mode, enabled, state, stt_utterance_count, languages, configured_languages, error, started_at}}` | All match statuses |
| `/api/matches/{id}/status` | GET | `{match_id, state, stt_utterance_count, languages, error, started_at}` | Single match status |
| `/api/matches/{id}/channels` | GET | `{match_id, appid, channels: {lang: {channel, token, uid}}}` | Viewer tokens for all configured languages; `original` is included for live matches and for demo matches only while the original pipeline is running |
| `/api/matches/{id}/transcript` | GET | `{match_id, transcript: [{text, ts, audio_start}]}` | Recent English STT text (last 50 utterances) |
| `/api/matches/{id}/detail` | GET | `{match_id, display_name, mode, enabled, auto_manage, kickoff_utc, state, keyterms, keyterms_source, log_dir, log_files, runs, match_meta, ...}` | Match config, keyterms, current log directory, and persisted match metadata |
| `/api/matches/{id}/logs/{stt\|lang}?tail=N` | GET | `{match_id, log_key, total_lines, rows: [...]}` | Tail structured JSONL logs (max 10000 lines) |
| `/api/matches/{id}/refresh-data` | POST | `{status, match_id, keyterm_count?, roster_player_count?, kickoff_utc?}` | Refresh Sportradar fixture data into `match_store` for the next live run |
| `/api/matches/{id}/start` | POST | match status JSON | Start a demo match |
| `/api/matches/{id}/stop` | POST | match status JSON | Stop a match |
| `/api/token` | POST | `{token, channel, uid, appid}` | Single viewer token (body: `{match_id, lang}`) |

### Static file routes

| Path | File | Purpose |
|---|---|---|
| `/` or `/control.html` | redirect | Redirects to `/status.html` (`control.html` retired) |
| `/status.html` | `status.html` | SportPortal ops home (authenticated) |
| `/login.html` | `login.html` | Operator login page |
| `/viewer_live.html` | `viewer_live.html` | Production viewer (connect overlay, language dropdown incl. original) |
| `/match_detail.html` | `match_detail.html` | Per-match detail page (STT + per-language log tabs, config, keyterms) |
| local file only | `viewer_test.html` | Standalone Agora viewer using `appid/channel/token/uid` query params |

All endpoints return JSON (except static files) with `Access-Control-Allow-Origin: *`.

### Match states

| State | Meaning |
|---|---|
| `idle` | Configured but not started |
| `starting` | Publishers launching, waiting for audio/video ready |
| `running` | STT active, all language pipelines publishing |
| `stopped` | Cleanly stopped (can be restarted) |
| `error` | Failed — check `error` field for details |

### Scheduler states

Returned by `/api/status/overview` for live matches:

| State | Meaning |
|---|---|
| `demo` | Demo match — never auto-managed |
| `upcoming` | Live match known but not near kickoff yet |
| `starting` | Scheduler has requested worker start |
| `running` | Worker is active |
| `finished` | Worker ran and then stopped |
| `stopped` | Disabled or manually stopped |
| `error` | Scheduler-side failure |

### Viewer UID allocation

Viewer UIDs start at 100 and increment globally across all requests. Publisher uses UID 73. Each `GET /api/matches/{id}/channels` or `POST /api/token` call allocates fresh UIDs so multiple viewers never collide.

## Production Agora Channel Contract

Each language in a match gets its own Agora channel.

### Channel naming

```
{match_id}-{lang}
```

Examples: `bmg_fch_demo-es`, `bmg_fch_demo-pt`, `bmg_fch_demo-fr`

### Original audio channel

In **demo mode**, a dedicated channel `{match_id}-original` is created by `_start_original_pipeline()`. It carries the source English commentary with video at zero delay (plays ahead of translated channels).

In **live mode**, the `/api/matches/{id}/channels` endpoint returns the resolved live source/original channel as the "original" entry. For `source.type = srt`, that is the internal ingest channel. For `source.type = srt_direct`, that is `source.original_channel`.

### Live SR data contract

Live mode currently uses Sportradar in two narrower ways:

- `/api/matches/{id}/refresh-data` and `server/scheduler.py` refresh lineup/summary-derived fixture data into `match_data/{match_id}/`
- `MatchWorker` loads roster text and keyterms from that stored data at startup

Live mode does **not** currently poll real-time SR commentary endpoints or inject SR events into output channels. The per-language live output channels are STT-only plus relayed source media:

- Agora live source: delayed video + delayed atmosphere + translated TTS
- SRT live source: delayed video + translated TTS only
- SRT direct live source: delayed video + translated TTS, plus delayed atmosphere when `source.atmosphere_audio_stream_index` is configured; Python STT reads local commentary PCM and `relay_publish` reads local cleaned H.264 plus optional atmosphere PCM, while original viewing uses a separate buffered Agora channel

### Publisher UID

| UID | Role | Publishes |
|---|---|---|
| 73 | Go publisher (per language channel) | H.264 video + PCM audio (TTS plus optional delayed atmosphere) |
| 100+ | Viewers (browser) | Nothing (audience role) |

### Channel profile

- Channel profile: live broadcasting
- Video codec: H.264
- Audio: PCM 16kHz mono via publisher stdin → Agora SDK
- Viewer token: v007 format, 24-hour expiry, generated per viewer via `server/token_api.py`
- Media publisher token: Agora RTC token with publisher privileges, 24-hour expiry, generated inside the Go publisher tools

### Standalone live test channel contract

`test_live_pipeline.py` uses the same live source contract as server live mode:

| Source UID | Meaning |
|---|---|
| 73 | video |
| 74 | atmosphere |
| 75 | commentary |

For a viewer-compatible test, the output channel should follow:

```text
{match_id}-{lang}
```

so that `viewer_live.html?match={match_id}&lang={lang}` can join it using the normal `/api/matches/{id}/channels` flow.

### `test_live_pipeline.py` CLI

Required:

- `--source-channel`
- `--output-channel`
- `--lang`

or use:

- `--test-id` to derive all three from a short id

Optional:

- `--video-uid` (default `73`)
- `--atmosphere-uid` (default `74`)
- `--commentary-uid` (default `75`)
- `--video-delay`
- `--start-margin`
- `--translation-model` (server default `gpt-5.4`; standalone test defaults may differ)
- `--max-stt-duration`
- `--match-id`
- `--sport-event-id`
- `--keyterms-file`
- `--viewer-base-url`
- `--viewer-test-path`
- `--write-test-config`
- `--prepare-only`

## Internal Log File Contract

These are internal runtime files written by `server/match_worker.py`, not HTTP endpoints.

### Directory layout

```text
match_data/{match_id}/runs/{YYYYMMDD_HHMMSS}/
  stt.jsonl
  {lang}.jsonl
```

`match_data/{match_id}/latest_run.txt` stores the newest run directory name.

### `stt.jsonl`

- First line: header object
- Later lines: one STT utterance per line

Header fields:

- `type="header"`
- `match_id`
- `mode`
- `started_at`
- `video_delay`
- `target_start`
- `languages`
- `keyterms`
- `roster`

Utterance fields:

- `type="utterance"`
- `audio_start`
- `audio_end`
- `wall_clock`
- `play_at`
- `text`

### `{lang}.jsonl`

- First line: header object for one language pipeline
- Later lines: one playback outcome per utterance

Header fields:

- `type="header"`
- `match_id`
- `language`
- `voice_id`
- `video_start`

Utterance fields:

- `type="utterance"`
- `source` (`stt` or `sr`)
- `uid` (STT utterance id, or `null` for SR)
- `audio_start`
- `audio_end`
- `play_at`
- `trans_ms`
- `tts_ms`
- `speed`
- `local_speed_factor`
- `intended_skew_ms`
- `fit_from_ms`
- `fit_to_ms`
- `fit_deadline_ms`
- `fit_cpu_ms`
- `status`
- `original`
- `translated`
- `play_duration_ms`

## Dev-Mode HTTP API (live_match.py)

Served by `ControlHandler` on port 8090 (configurable via `--lang-port`).

### Session management

| Endpoint | Method | Params | Response | Purpose |
|---|---|---|---|---|
| `/api/session` | POST | `?lang=XX` (optional) | `{sessionId, channel, token, appid}` | Create new session |
| `/api/session/{id}/start` | POST | none | `{"status":"starting"}` | Start session pipeline |
| `/api/session/{id}/stop` | POST | none | `{"status":"stopping"}` | Stop session pipeline |
| `/api/session/{id}/set-lang` | GET | `?lang=XX` | `{"lang":"XX"}` | Change session language |
| `/api/session/{id}/set-atmosphere` | GET | `?enabled=true\|false` | `{"atmosphere":bool}` | Toggle atmosphere audio |
| `/api/session/{id}/set-original` | GET | `?enabled=true\|false` | `{"original":bool}` | Toggle original audio pass-through |
| `/api/session/{id}/status` | GET | none | `{"running":bool,"lang":"XX","atmosphere":bool,"original":bool}` | Session state |

### Static file serving

| Endpoint | Method | Response | Purpose |
|---|---|---|---|
| `/viewer.html` | GET | HTML | Serves the dev-mode viewer page |

All endpoints return JSON (except static files) with `Access-Control-Allow-Origin: *`.

## PCM Audio Format

| Field | Value |
|---|---|
| Encoding | 16-bit signed little-endian (S16LE) |
| Sample rate | 16,000 Hz |
| Channels | 1 (mono) |
| Chunk size | 320 bytes (10ms) |
| Bytes per second | 32,000 |

The TTSEngine splits ElevenLabs audio into 10ms chunks and writes them to the Go publisher's stdin at a steady 10ms rate.

## Atmosphere Audio

Stadium atmosphere (crowd noise, whistles, chants) separated from the original broadcast via Mel-Band Roformer. Loaded as raw PCM and mixed into every output chunk.

| Field | Value |
|---|---|
| Source format | 16kHz mono S16LE WAV (same as TTS) |
| Default volume | 0.5x |
| Mixing | Per-sample addition with int16 clamping |
| Position sync | Synced to video time on toggle (not from file start) |
| Looping | Wraps to start when file ends |
| CLI flag | `--atmosphere path/to/atmosphere.wav` (dev mode) |
| YAML field | `atmosphere: path/to/atmosphere.wav` (server mode) |

When enabled, atmosphere is mixed into both TTS/SR audio and silence (continuous crowd noise).

## Original Audio Pass-Through

Plays the source English commentary audio synced to video, bypassing TTS translation.

| Field | Value |
|---|---|
| Source | `--audio` file, converted to 16kHz mono PCM at startup |
| Position sync | Synced to video time on toggle (`elapsed * 32000`) |
| Controls | Disables lang select and atmosphere toggle in viewer |
| API | `/api/session/{id}/set-original?enabled=true` (dev mode only) |

When enabled, `_pipe_writer` writes original audio chunks at 10ms rate instead of TTS/SR output. STT and translation still run in background and resume when toggled off.

## Events File Format

```
# Comment lines start with #
# Blank lines are ignored
offset|PRIORITY|message
```

| Field | Format | Example |
|---|---|---|
| offset | Seconds (int) or `mm:ss` | `120` or `2:00` |
| priority | `INTERRUPT` or `APPEND` | `INTERRUPT` |
| message | English text | `Goal! Honorat scores!` |

`INTERRUPT` events clear the TTS queue before speaking. `APPEND` events queue normally.

## ElevenLabs WebSocket Protocol

Connection URI pattern:
```
wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id={model}&output_format=pcm_16000
```

Message sequence:
1. Send initial config: `{"text": " ", "voice_settings": {"speed": 1.0, "stability": 1.0, "similarity_boost": 1.0}, "xi_api_key": "..."}`
2. Send text: `{"text": "...", "try_trigger_generation": true}`
3. Send flush: `{"text": ""}`
4. Receive audio chunks: `{"audio": "base64...", "isFinal": false}`
5. Receive final: `{"isFinal": true}`

## Sportradar API

Base URL: `https://api.sportradar.com/soccer-extended/trial/v4/en`

| Endpoint | Purpose |
|---|---|
| `sport_events/{id}/timeline.json` | Play-by-play commentary events |
| `sport_events/{id}/insights.json` | AI-generated match insights |
| `sport_events/{id}/fun_facts.json` | AI-generated fun facts |
| `sport_events/{id}/lineups.json` | Player rosters for name correction |

Authentication: `x-api-key` header.

## Avatar Backend `/speak` Endpoint

Used by `commentary_feeder.py` and `match_replay.py`:

```json
POST /speak
{
    "agent_id": "...",
    "text": "Goal by Honorat!",
    "priority": "INTERRUPT"
}
```

This pushes text to an Agora Conversational AI avatar's TTS queue.

## Live Mode Contracts

### subscribe_audio.go CLI Contract

```bash
go run subscribe_audio.go <app_id> <source_channel> <uid_to_subscribe>
# Subscribes to source Agora channel
# Writes UID 75 (commentary) PCM to stdout (S16LE 16kHz mono)
# Python reads stdout as if it were a file-based audio stream
```

Environment: `AGORA_APP_CERTIFICATE`, `DYLD_LIBRARY_PATH`

### relay_publish.go CLI Contract

```bash
go run relay_publish.go <app_id> <source_channel> <output_channel> <video_delay>
# Subscribes to source channel UIDs 73 (video) + 74 (atmosphere)
# Delay-buffers video and atmosphere for video_delay seconds
# Reads translated TTS PCM from stdin
# Publishes to output channel: delayed video + mixed audio (delayed atmos + TTS)
# UID 75 (commentary) is NOT relayed to output
```

Environment: `AGORA_APP_CERTIFICATE`, `DYLD_LIBRARY_PATH`

One `relay_publish` process runs per language.

### SRT Direct Local Source Contract

For `source.type = srt_direct`, the server starts one `publish_srt_to_agora.py` / Go source process. That process owns the only SRT connection and exposes local sockets for the rest of the pipeline. This path exists because the remote SRT feed needs explicit FFmpeg/libav pull, audio-stream selection, and H.264 cleanup before RTC publishing:

| Local source | Consumer | Content |
|---|---|---|
| `pcm` | Python STT | selected commentary/program audio, 16kHz mono S16LE |
| `atmos_pcm` | per-language `relay_publish.go` | selected atmosphere audio, 16kHz mono S16LE |
| `video` | per-language `relay_publish.go` | cleaned H.264 access units: Annex B, no `AUD`/filler NALs, SPS/PPS carried before keyframes |

The source process also publishes `source.original_channel` for viewer original mode. That channel carries encoded video plus a commentary/atmosphere mix delayed by `source.original_buffer_seconds`.

### Live Mode Source Channel Layout

| Source UID | Content | Subscribed by |
|---|---|---|
| 73 | Live video | `relay_publish.go` |
| 74 | Stadium atmosphere | `relay_publish.go` |
| 75 | Live commentary | `subscribe_audio.go` |

### Live Mode Output Channel Layout

| Output UID | Content |
|---|---|
| 73 | Delayed video (from source UID 73) + mixed audio (delayed atmosphere + translated TTS) |

No UID 75 in output — original commentary is replaced by translated TTS.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — ElevenLabs WebSocket protocol details and buffer strategy

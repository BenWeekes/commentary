# L1 — Interfaces

> Production server API, Agora channel contracts, PCM audio format, dev-mode API, and planned contracts.

## Production Server HTTP API

Served by `StatusHandler` in `server/status_api.py` on port 8080 (configurable via `control_port` in YAML).

### Match management

| Endpoint | Method | Response | Purpose |
|---|---|---|---|
| `/api/matches` | GET | `{match_id: {match_id, state, stt_utterance_count, languages, configured_languages, error, started_at}}` | All match statuses |
| `/api/matches/{id}/status` | GET | `{match_id, state, stt_utterance_count, languages, error, started_at}` | Single match status |
| `/api/matches/{id}/channels` | GET | `{match_id, appid, channels: {lang: {channel, token, uid}}}` | Viewer tokens for all configured languages |
| `/api/matches/{id}/transcript` | GET | `{match_id, transcript: [{text, ts, audio_start}]}` | Recent English STT text (last 50 utterances) |
| `/api/matches/{id}/start` | POST | match status JSON | Start a demo match |
| `/api/matches/{id}/stop` | POST | match status JSON | Stop a match |
| `/api/token` | POST | `{token, channel, uid, appid}` | Single viewer token (body: `{match_id, lang}`) |

### Static file routes

| Path | File | Purpose |
|---|---|---|
| `/` or `/control.html` | `control.html` | Admin control page |
| `/status.html` | `status.html` | Public status dashboard |
| `/viewer_live.html` | `viewer_live.html` | Production viewer |

All endpoints return JSON (except static files) with `Access-Control-Allow-Origin: *`.

### Match states

| State | Meaning |
|---|---|
| `idle` | Configured but not started |
| `starting` | Publishers launching, waiting for audio/video ready |
| `running` | STT active, all language pipelines publishing |
| `stopped` | Cleanly stopped (can be restarted) |
| `error` | Failed — check `error` field for details |

### Viewer UID allocation

Viewer UIDs start at 100 and increment globally across all requests. Publisher uses UID 73. Each `GET /api/matches/{id}/channels` or `POST /api/token` call allocates fresh UIDs so multiple viewers never collide.

## Production Agora Channel Contract

Each language in a match gets its own Agora channel.

### Channel naming

```
{match_id}-{lang}
```

Examples: `bmg_fch_demo-es`, `bmg_fch_demo-pt`, `bmg_fch_demo-fr`

### Publisher UID

| UID | Role | Publishes |
|---|---|---|
| 73 | Go publisher (per language channel) | H.264 video + PCM audio (TTS + atmosphere) |
| 100+ | Viewers (browser) | Nothing (audience role) |

### Channel profile

- Channel profile: live broadcasting
- Video codec: H.264
- Audio: PCM 16kHz mono via publisher stdin → Agora SDK
- Token: v007 format, 24-hour expiry, generated per viewer via `server/token_api.py`

## Internal Log File Contract

These are internal runtime files written by `server/match_worker.py`, not HTTP endpoints.

### Directory layout

```text
logs/{match_id}_{YYYYMMDD_HHMMSS}/
  stt.jsonl
  {lang}.jsonl
```

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
- `play_at`
- `xlat_ms`
- `tts_ms`
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
1. Send initial config: `{"text": " ", "voice_settings": {...}, "xi_api_key": "..."}`
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

## Planned Contracts

The following interfaces are **planned** for live match mode and do not exist yet.

### subscribe_audio.go CLI Contract (Planned)

```bash
go run subscribe_audio.go <app_id> <source_channel> <uid_to_subscribe>
# Subscribes to source Agora channel
# Writes UID 75 (commentary) PCM to stdout (S16LE 16kHz mono)
# Python reads stdout as if it were a file-based audio stream
```

Environment: `AGORA_APP_CERTIFICATE`, `DYLD_LIBRARY_PATH`

### relay_publish.go CLI Contract (Planned)

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

### Live Mode Source Channel Layout (Planned)

| Source UID | Content | Subscribed by |
|---|---|---|
| 73 | Live video | `relay_publish.go` |
| 74 | Stadium atmosphere | `relay_publish.go` |
| 75 | Live commentary | `subscribe_audio.go` |

### Live Mode Output Channel Layout (Planned)

| Output UID | Content |
|---|---|
| 73 | Delayed video (from source UID 73) + mixed audio (delayed atmosphere + translated TTS) |

No UID 75 in output — original commentary is replaced by translated TTS.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — ElevenLabs WebSocket protocol details and buffer strategy

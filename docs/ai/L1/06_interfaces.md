# L1 — Interfaces

## Control HTTP API (Multi-Session)

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
| `/viewer.html` | GET | HTML | Serves the viewer page |

All endpoints return JSON (except static files) with `Access-Control-Allow-Origin: *`.

## Agora Channel Contract

Each session gets its own channel (`commentary-{uuid[:8]}`).

| UID | Role | Publishes |
|---|---|---|
| 73 | Go publisher (per session) | H.264 video + PCM audio (TTS) |
| Viewer UID | Viewer (browser) | Nothing (audience role) |

- Viewer UID: returned in session creation response
- Channel profile: live broadcasting
- Video codec: H.264
- Audio: PCM 16kHz mono via publisher stdin → Agora SDK
- Token: v007 format, 1-hour expiry, generated per session via `tokens.py`

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
| CLI flag | `--atmosphere path/to/atmosphere.wav` |

When enabled, atmosphere is mixed into both TTS/SR audio and silence (continuous crowd noise). Toggle via `/api/session/{id}/set-atmosphere?enabled=true`.

## Original Audio Pass-Through

Plays the source English commentary audio synced to video, bypassing TTS translation.

| Field | Value |
|---|---|
| Source | `--audio` file, converted to 16kHz mono PCM at startup |
| Position sync | Synced to video time on toggle (`elapsed * 32000`) |
| Controls | Disables lang select and atmosphere toggle in viewer |
| API | `/api/session/{id}/set-original?enabled=true` |

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

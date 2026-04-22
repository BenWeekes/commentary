# L1 — Interfaces

## Control HTTP API

Served by `ControlHandler` on port 8090 (configurable via `--lang-port`).

| Endpoint | Method | Params | Response | Purpose |
|---|---|---|---|---|
| `/set-lang` | GET | `?lang=XX` | `{"lang":"XX"}` | Change commentary language |
| `/start` | GET | none | `{"status":"starting"}` | Start the pipeline |
| `/stop` | GET | none | `{"status":"stopping"}` | Stop the pipeline |
| `/status` | GET | none | `{"running":bool,"lang":"XX"}` | Current state |

All endpoints return JSON with `Access-Control-Allow-Origin: *`.

## Agora Channel Contract

| UID | Role | Publishes |
|---|---|---|
| 73 | Go publisher | H.264 video + PCM audio (TTS) |
| 101 | Viewer (browser) | Nothing (audience role) |

- Channel profile: live broadcasting
- Video codec: H.264
- Audio: PCM 16kHz mono via publisher stdin → Agora SDK

## PCM Audio Format

| Field | Value |
|---|---|
| Encoding | 16-bit signed little-endian (S16LE) |
| Sample rate | 16,000 Hz |
| Channels | 1 (mono) |
| Chunk size | 320 bytes (10ms) |
| Bytes per second | 32,000 |

The TTSEngine splits ElevenLabs audio into 10ms chunks and writes them to the Go publisher's stdin at a steady 10ms rate.

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

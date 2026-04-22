# L1 — Workflows

## Run Modes

### Events only (simplest — no API keys except OpenAI)

```bash
python3 live_match.py \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es --autostart
```

Replays pre-timed events through TTS. No STT, no video, no Agora.

### STT + Events (audio translation)

```bash
python3 live_match.py \
    --audio data/audio/bmg_fch_first_5min.mp3 \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es --autostart
```

Requires: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`

### Full demo (video + STT + events → Agora)

```bash
python3 live_match.py \
    --audio data/audio/bmg_fch_first_5min.mp3 \
    --video-h264 go-audio-video-publisher/encoded_assets/bundesliga.h264 \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es --channel sportradar-live
```

Requires: All API keys + Go publisher + Agora SDK

### Viewer-controlled (start/stop from browser)

```bash
# Terminal 1: start server (waits for /start)
python3 live_match.py \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es

# Terminal 2 (or browser): open viewer
open "viewer.html?appid=APP_ID&channel=sportradar-live&token=TOKEN&lang=es"
# Click "Start" in the viewer
```

### STT benchmark

```bash
python3 stt_realtime_translate.py \
    --audio data/audio/bmg_fch_first_5min.mp3 \
    --lang es
```

Measures per-utterance latency: STT time, translation time, total pipeline latency.

### Commentary feeder (live Sportradar)

```bash
python3 commentary_feeder.py \
    --agent-id AGENT_ID \
    sr:sport_event:69339340 \
    --lang es
```

Polls Sportradar Extended API every 5s and pushes commentary to an Agora avatar.

### Match replay (avatar)

```bash
python3 match_replay.py \
    --agent-id AGENT_ID \
    --events data/events/replay_39_45.txt \
    --speed 2 --lang es
```

## Adding a New Language

1. Add language code and name to `LANG_NAMES` dict in `live_match.py` (and optionally in `commentary_feeder.py`, `match_replay.py`, `stt_realtime_translate.py`)
2. Optionally add an ElevenLabs voice ID to `LANG_VOICES` dict
3. Add an `<option>` element in `viewer.html` language dropdown
4. No translation prompt changes needed — GPT-4o-mini handles any language

## Generating an Agora Token

```python
from tokens import AccessToken, ServiceRtc, ServiceRtm

token = AccessToken("APP_ID", "APP_CERT")

# RTC privileges
rtc = ServiceRtc("channel-name", 101)
rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, 0)
rtc.add_privilege(ServiceRtc.kPrivilegePublishAudioStream, 0)
token.add_service(rtc)

# Optional: RTM privileges
rtm = ServiceRtm("101")
rtm.add_privilege(ServiceRtm.kPrivilegeLogin, 0)
token.add_service(rtm)

print(token.build())
```

Privilege expire value of `0` means no expiry. For production, use `int(time.time()) + 3600`.

## Switching Language at Runtime

```bash
# Via curl
curl "http://localhost:8090/set-lang?lang=fr"

# Via viewer
# Select language from dropdown — sends /set-lang automatically
```

Language changes take effect on the next TTS utterance (JIT translation).

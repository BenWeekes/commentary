# L1 — Code Map

> Directory layout, file purposes, and module-level maps for every script in the repo.

## Directory Tree

```
commentary/
├── live_match.py                  # Main orchestrator (2226 lines)
├── commentary_feeder.py           # Sportradar API poller → avatar agent
├── match_replay.py                # Events file replay → avatar agent
├── stt_realtime_translate.py      # STT latency benchmark
├── viewer.html                    # Agora Web SDK viewer + controls
├── tokens.py                      # Agora v007 token generation
├── requirements.txt               # Python dependencies
├── .env.example                   # API key template
├── data/
│   ├── events/                    # Match event files (offset|priority|text)
│   │   ├── bmg_fch_md28_full_match.txt
│   │   ├── bmg_fch_35_40_clip.txt # Synced clip: match 35:00-40:00
│   │   ├── replay_*.txt
│   │   └── *_commentary.txt
│   ├── audio/                     # Commentary audio samples
│   │   ├── bmg_fch_match_35_40.mp3  # Synced: match 35:00-40:00 (16kHz mono)
│   │   └── bmg_fch_first_5min.mp3   # Pre-match content (first 5 min of file)
│   └── json/                      # Full Sportradar API responses
│       ├── bmg_fch_md28_all_data.json
│       ├── bayern_real_madrid_2nd_leg.json
│       └── real_madrid_bayern_all_data.json
├── go-audio-video-publisher/      # Go H.264+PCM → Agora publisher
│   ├── main.go                    # Publisher entry point (1211 lines)
│   ├── decode_media.c/h           # FFmpeg C bindings
│   ├── go.mod, go.sum             # Go module (has local replace directive)
│   ├── Makefile
│   └── reference/agora_go_sdk/    # Standalone Go sender examples
│       ├── send_h264_pcm_uid73.go # H.264 video + PCM stdin audio
│       ├── send_h264_uid73.go     # H.264 video only
│       └── send_encoded_audio_uid74.go
└── docs/ai/                       # Progressive disclosure docs
```

## Module Map — live_match.py

| Section | Lines | Purpose |
|---|---|---|
| `_load_dotenv()` | 75–90 | Load `.env` into `os.environ` |
| Config constants | 92–111 | App IDs, delays, audio params |
| `TERMS_LIST` | 115–134 | Deepgram keyword boosting (~67 terms) |
| `CORRECTIONS` | 138–181 | Deterministic STT error corrections (~42 entries) |
| `LANG_NAMES`, `LANG_VOICES` | 192–209 | 12 languages, 8 with dedicated ElevenLabs voice IDs |
| `ControlHandler` | 317–517 | HTTP server for multi-session viewer control |
| `translate_text()` | 548–559 | GPT-4o-mini translation call |
| `TTSEngine` | 574–1196 | ElevenLabs WebSocket TTS + PCM buffering + lookahead + atmosphere mixing |
| `load_atmosphere()` | 1519–1526 | Load 16kHz mono WAV as raw PCM bytes |
| Audio helpers | 1531–1567 | ffmpeg conversion, real-time PCM chunking |
| `start_publisher()` | 1569–1595 | Launch Go publisher subprocess |
| Events fallback | 1735–1836 | Load and replay Sportradar events file |
| STT pipeline | 1841–1986 | Deepgram WebSocket → corrections → forced split → translate |
| `run_pipeline_for_session()` | 1991–2130 | Orchestrate one session pipeline |
| `main()` | 2132–2226 | CLI args, control server, main loop |

## Module Map — commentary_feeder.py

| Section | Lines | Purpose |
|---|---|---|
| Translation | 42–93 | GPT-4o-mini translator (same as live_match) |
| `sportradar_get()` | 102–107 | HTTP GET to Sportradar Extended API |
| `speak()` | 110–128 | POST to avatar backend `/speak` endpoint |
| `feed_match()` | 213–306 | Main polling loop — timeline + insights |
| `replay_file()` | 309–360 | Replay saved commentary file |

## Module Map — tokens.py

| Class | Purpose |
|---|---|
| `Service` | Base class — packs service type + privileges |
| `ServiceRtc` | RTC privileges (join, publish audio/video/data) |
| `ServiceRtm` | RTM privileges (login) |
| `AccessToken` | Token builder — HMAC-SHA256 signing, zlib compression |
| `build_token_with_rtm()` | Convenience function for RTC+RTM tokens |

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — detailed breakdown of `TTSEngine` class (lines 574–1196)
- [STT Pipeline](L2/stt_pipeline.md) — Deepgram integration and correction system

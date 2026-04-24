# L1 — Code Map

## Directory Tree

```
commentary/
├── live_match.py                  # Main orchestrator (1038 lines)
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
| `_load_dotenv()` | 67–82 | Load `.env` into `os.environ` |
| Config constants | 86–103 | App IDs, delays, audio params |
| `TERMS_LIST` | 107–126 | Deepgram keyword boosting |
| `CORRECTIONS` | 130–173 | Deterministic STT error corrections |
| `LANG_NAMES`, `LANG_VOICES` | 184–201 | Language config, ElevenLabs voice IDs |
| `ControlHandler` | 215–297 | HTTP server for viewer control |
| `translate_text()` | 309–320 | GPT-4o-mini translation call |
| `TTSEngine` | 330–600 | ElevenLabs WebSocket TTS + PCM buffering + atmosphere mixing |
| `load_atmosphere()` | ~1300 | Load 16kHz mono WAV as raw PCM bytes |
| Audio helpers | ~1315 | ffmpeg conversion, real-time PCM chunking |
| `start_publisher()` | 629–659 | Launch Go publisher subprocess |
| Events fallback | 674–786 | Load and replay Sportradar events file |
| STT pipeline | 791–876 | Deepgram WebSocket → corrections → translate |
| `run_pipeline()` | 881–941 | Orchestrate one pipeline cycle |
| `main()` | 944–1038 | CLI args, control server, main loop |

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

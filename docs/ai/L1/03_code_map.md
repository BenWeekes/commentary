# L1 — Code Map

> Directory layout, file purposes, and module-level maps for every script in the repo.

## Directory Tree

```
commentary/
├── test_live_pipeline.py           # Standalone one-language live pipeline smoke test
├── live_match.py                  # Dev-mode orchestrator (~940 lines)
├── commentary_feeder.py           # Sportradar API poller → avatar agent
├── match_replay.py                # Events file replay → avatar agent
├── stt_realtime_translate.py      # STT latency benchmark (~350 lines)
├── generate_demo_transcript.py   # Demo transcript generator (STT + SR + translate)
├── viewer.html                    # Dev-mode Agora Web SDK viewer + controls
├── tokens.py                      # Agora v007 token generation
├── requirements.txt               # Python dependencies
├── .env.example                   # API key template
├── matches.yaml                   # Server mode match configuration
├── matches_live_test.yaml         # Viewer-compatible one-match config for standalone live tests
├── status.html                    # Public match status page (read-only dashboard)
├── control.html                   # Admin control page (start/stop matches)
├── viewer_test.html               # Standalone file-based Agora viewer (takes appid/channel/token/uid in query params)
├── viewer_live.html               # Production viewer (multi-match, lang select)
├── match_data/                    # Per-match persistent data: keyterms, metadata, run logs
├── logs/                          # Legacy runtime log location kept for older runs / tooling
├── server/                        # Production server package
│   ├── __init__.py                # Package marker
│   ├── main.py                    # Entry point: arg parse, signal handling
│   ├── config.py                  # MatchConfig, ServerConfig, YAML loader
│   ├── match_store.py             # Per-match disk store: keyterms, metadata, run dirs
│   ├── orchestrator.py            # Orchestrator: start/stop/query MatchWorkers
│   ├── scheduler.py               # Live-match scheduler: SR refresh cadence, kickoff countdown, auto-start
│   ├── match_worker.py            # MatchWorker: 1 STT → N language pipelines (~530 lines)
│   ├── sr_data.py                 # Sportradar refresh helpers: lineups/summary fetch, roster/keyterms derivation
│   ├── status_api.py              # HTTP API + static file serving
│   └── token_api.py               # generate_viewer_token()
├── lib/                           # Shared library (extracted from live_match.py)
│   ├── __init__.py                # Package marker
│   ├── constants.py               # SAMPLE_RATE, BYTES_PER_10MS, timing, ElevenLabs defaults
│   ├── corrections.py             # TERMS_LIST, CORRECTIONS, apply_corrections()
│   ├── translator.py              # LANG_NAMES, LANG_VOICES, voice_for_lang(), translate_text()
│   ├── audio.py                   # load_atmosphere(), convert_to_pcm(), pcm_chunks_realtime()
│   ├── events.py                  # load_events_file() — parse offset|PRIORITY|message files
│   ├── tts_engine.py              # TTSEngine class + _ts() helper (~648 lines)
│   ├── sr_prefetcher.py           # SRPrefetcher class (~327 lines)
│   └── stt_pipeline.py            # run_stt_pipeline(), run_stt_pipeline_multi() (~240 lines)
├── data/
│   ├── events/                    # Match event files (offset|priority|text)
│   ├── audio/                     # Commentary audio samples
│   └── json/                      # Full Sportradar API responses
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

## Module Map — server/ (production server)

| Module | Contents | Dependencies |
|---|---|---|
| `server/main.py` | `_load_dotenv()`, `main()` — CLI args, config load, orchestrator init, scheduler startup, signal handling | `server.config`, `server.orchestrator`, `server.status_api` |
| `server/config.py` | `MatchConfig` (dataclass), `ServerConfig` (dataclass), `_resolve_path()`, `load_config()`, `validate_config()` | `yaml` (pyyaml) |
| `server/match_store.py` | `MatchStore` class — persistent per-match folder management, atomic JSON writes, keyterms I/O, run directory creation / listing | `json`, `os`, `time` |
| `server/orchestrator.py` | `Orchestrator` class — owns `MatchStore`, per-match worker locks, `Scheduler`, `start_match()`, `stop_match()`, `get_all_status()`, `get_worker()` | `server.match_store`, `server.match_worker`, `server.scheduler` |
| `server/scheduler.py` | `MatchSchedule` (dataclass), `Scheduler` class — refresh cadence, kickoff countdown, auto-start/stop state tracking for live matches | `server.config`, `server.sr_data` |
| `server/match_worker.py` | `LangTelemetry`, `MatchStatus` (dataclasses), `_LangPipeline`, `_start_publisher()`, `_wait_for_publisher_signal()`, `_kill_publisher()`, `MatchWorker` class — match lifecycle, STT fan-out, structured JSONL log creation (`_setup_log_dir()`, `_open_stt_log()`, `_open_lang_log()`), telemetry aggregation (`_on_telemetry()`), cleanup | `lib.*`, `server.config`, `server.match_store`, `openai` |
| `server/sr_data.py` | `fetch_lineups()`, `fetch_summary()`, `derive_roster()`, `derive_keyterms()`, `refresh_match_data()` — fixture refresh path for live matches | `urllib.request`, `json`, `time` |
| `server/status_api.py` | `StatusHandler` (HTTP handler), `start_status_server()` — GET/POST routes for match status, scheduler overview, channels, transcript, detail, refresh-data, static files | `server.token_api`, `server.sr_data` |
| `server/token_api.py` | `generate_viewer_token()` — Agora v007 audience-only token | `tokens` |

## Module Map — lib/ (shared library)

| Module | Contents | Dependencies |
|---|---|---|
| `lib/constants.py` | `SAMPLE_RATE`, `CHANNELS`, `BYTES_PER_10MS`, `VIDEO_DELAY_S`, `MAX_LATENCY_S`, `SILENCE_GAP_S`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | standalone |
| `lib/corrections.py` | `TERMS_LIST` (91 terms), `CORRECTIONS` (60 pairs), `apply_corrections()` | standalone |
| `lib/translator.py` | `LANG_NAMES` (12 languages), `LANG_VOICES`, `DEFAULT_VOICE_ID`, `voice_for_lang()`, `TRANSLATE_SYSTEM` (6 rules), `TRANSLATE_SYSTEM_WITH_ROSTER` (roster-aware, 7 rules), `translate_text()` — supports `gpt-4o-mini` and `gpt-5.4-mini` | standalone (takes `oai_client` param) |
| `lib/audio.py` | `load_atmosphere()`, `convert_to_pcm()`, `pcm_chunks_realtime()` | standalone (stdlib only) |
| `lib/events.py` | `load_events_file()` — parses `offset\|PRIORITY\|message` format | standalone |
| `lib/tts_engine.py` | `_ts()` helper, `TTSEngine` class — TTS worker, pipe writer, lookahead, telemetry metadata slots (`_playback_meta_slot`, `_sr_playback_meta_slot`, `_skipped_meta`), two-phase shutdown (`_closing` + `_stop`) | `lib.constants` |
| `lib/sr_prefetcher.py` | `SRPrefetcher` class — SR prefetch, scheduling, metadata propagation into `TTSEngine` SR telemetry | `lib.constants`, `lib.tts_engine` |
| `lib/stt_pipeline.py` | `_run_stt_core()` (shared Deepgram logic), `run_stt_pipeline()` (single-lang), `run_stt_pipeline_multi()` (multi-lang fan-out) | `lib.corrections`, `lib.translator`, `lib.audio`, `lib.tts_engine` |

## Module Map — live_match.py (dev-mode orchestrator)

| Section | Lines | Purpose |
|---|---|---|
| Imports from `lib/` | 66–71 | Constants, translator, audio, TTSEngine, SRPrefetcher, STT pipeline |
| `_load_dotenv()` | 75–93 | Load `.env` into `os.environ` |
| Config globals | 94–104 | `AGORA_APP_ID`, `AGORA_APP_CERT`, `ELEVENLABS_API_KEY`, `SPORTRADAR_API_KEY` |
| `get_current_lang()` | 102–115 | File-based language switching (demo-specific) |
| `Session`, `SessionManager` | 125–203 | Multi-session viewer management |
| `ControlHandler` | 202–406 | HTTP server for session control API |
| `start_publisher()` | 430–462 | Launch Go publisher subprocess |
| `_wait_for_publisher_audio/video()` | 465–551 | Publisher startup synchronization |
| `load_events_file()` | 567–584 | Parse Sportradar events file |
| `run_events_fallback()` | 596–698 | Replay events with parallel TTS prefetching |
| `run_pipeline_for_session()` | 702–842 | Orchestrate one session pipeline |
| `main()` | 845–943 | CLI args, control server, main loop |

## Module Map — test_live_pipeline.py

| Section | Purpose |
|---|---|
| `_load_dotenv()` | Loads repo-root `.env` before any API calls |
| `_read_keyterms()` | Chooses explicit keyterms file, per-match keyterms, or falls back to `TERMS_LIST` |
| `_fetch_roster()` | Optional Sportradar lineup fetch for roster-aware translation |
| `_go_program_cmd()` | Uses built Go binaries if present, otherwise falls back to `go run` |
| `_write_test_config()` | Writes a one-match `matches_live_test.yaml` for `viewer_live.html` |
| `main()` | Parses CLI args, launches `subscribe_audio` and `relay_publish`, runs live STT fan-out for one language, prints viewer URL when output channel matches `{match_id}-{lang}` |

## Module Map — generate_demo_transcript.py

| Section | Purpose |
|---|---|
| `fetch_roster()` | Fetches player roster from Sportradar lineups API for GPT prompt |
| `get_stt_utterances()` | Streams audio through Deepgram Nova-3 with keyterms, returns `(time_s, text)` |
| `get_sr_events()` | Loads SR events from `events.txt` file |
| `save_stt_cache()` / `load_stt_cache()` | JSON cache for STT+SR results (avoids re-running Deepgram) |
| `translate_all()` | Translates entries to 5 languages in parallel (ThreadPoolExecutor, 5 workers) |
| `write_english_only()` | Writes English-only transcript in `time : lang : SR/STT : text` format |
| CLI modes | `--stt-only` (STT + cache), `--translate` (from cache), default (full pipeline) |

Output files: `demo_transcript_en.txt` (English), `demo_transcript.txt` (6 languages), `demo_stt_cache.json` (cache), `demo_llm_comparison.txt` (model comparison).

## Module Map — stt_realtime_translate.py (benchmark)

| Section | Purpose |
|---|---|
| Imports from `lib/` | `TERMS_LIST`, `CORRECTIONS`, `apply_corrections`, `LANG_NAMES`, `convert_to_pcm`, `pcm_chunks_realtime` |
| `TRANSLATE_SYSTEM` (own) | 5-rule version for benchmarking (intentionally different from lib's 7-rule version) |
| `translate_utterance()` | Translates with `temp=0.2`, returns `(text, latency)` tuple |
| `run_pipeline()` | Deepgram STT → corrections → translate → latency measurement |
| `print_report()` | Terminal summary with per-utterance and aggregate stats |

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

## Go Live Mode Programs

| File | Lines | Purpose |
|---|---|---|
| `go-audio-video-publisher/cmd/subscribe_audio/main.go` | ~229 | Subscribes to source Agora channel, writes UID 75 (commentary) PCM to stdout for Python STT. Signals readiness on stderr. |
| `go-audio-video-publisher/cmd/relay_publish/main.go` | ~599 | Subscribes to source channel UIDs 73 (video) + 74 (atmosphere), delay-buffers both, reads TTS PCM from stdin, mixes delayed atmosphere + TTS, publishes to per-language output channel. Signals readiness on stdout. |

## Runtime Log Files

Server mode writes structured JSONL logs under:

```text
match_data/{match_id}/runs/{YYYYMMDD_HHMMSS}/
```

Per run:

- `stt.jsonl` — one shared log for the match STT pipeline
- `{lang}.jsonl` — one per-language playback/translation log
- `match_data/{match_id}/latest_run.txt` — newest run pointer
These files are created by `server.match_worker.MatchWorker` and are intended for post-match analysis, not user-facing APIs. Older historical runs may still exist under the legacy top-level `logs/` directory.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — detailed breakdown of `TTSEngine` class in `lib/tts_engine.py`
- [STT Pipeline](L2/stt_pipeline.md) — Deepgram integration, correction system, and multi-lang fan-out in `lib/stt_pipeline.py`

# L1 — Conventions

> Naming rules, audio format constants, event file syntax, YAML config format, and runtime behaviours like JIT translation.

## Naming

- Python files: `snake_case.py`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Classes: `PascalCase` (e.g., `TTSEngine`, `ControlHandler`, `MatchWorker`)
- Go files: `snake_case.go` in `reference/`, `main.go` at root
- Events files: descriptive names with underscores (e.g., `bmg_fch_md28_full_match.txt`)
- Match IDs: lowercase with underscores (e.g., `bmg_fch_demo`)

## Voice IDs

Per-language ElevenLabs voice mapping in `lib/translator.py` (`LANG_VOICES` dict). Languages without an entry fall back to `DEFAULT_VOICE_ID`.

Voice selection is just-in-time: `voice_for_lang()` is called at TTS time, not at queue time, so language changes take effect on the next utterance.

## Pass Filtering

Simple pass events like "to Diks." or "Elvedi to Nicolas." can be identified by the regex `_PASS_RE` and the helper `_is_simple_pass()`. These are defined but not currently active — no filtering is applied at runtime. All events are passed through regardless.

## JIT Translation

Translation is deferred to TTS time, not queue time. This means:
- The `speak()` method accepts a `translate_fn` callback
- The TTS worker calls `translate_fn(text)` just before TTS generation
- Language changes via `/set-lang` take effect on the very next utterance
- The translate function returns `(translated_text, voice_id)` tuple

## Name Correction Strategy

Two approaches are used depending on context:

### Static corrections (legacy, per-match)

The `CORRECTIONS` list in `lib/corrections.py` (~60 entries) fixes systematic Deepgram misrecognitions via string replacement. Each correction is a `(wrong, right)` tuple applied in order. Used by `stt_realtime_translate.py` and the live pipeline.

### Dynamic roster-based correction (preferred)

For any match, the player roster is fetched from Sportradar's lineups API pre-match. The roster is injected into the GPT translation prompt (`TRANSLATE_SYSTEM_WITH_ROSTER`), enabling GPT to fix STT name errors during translation without a per-match corrections list. This scales to any game automatically.

The `TERMS_LIST` for Deepgram keyterm boosting can also be generated dynamically from the lineups API (full names + surnames + team/venue/referee names).

## Events File Format

```
# Comments start with #
offset_seconds|PRIORITY|message text
```

- `offset_seconds`: integer or `mm:ss` format
- `PRIORITY`: `INTERRUPT` (high priority, clears queue) or `APPEND` (normal)
- `message text`: English commentary text

## Match Config Format (YAML)

Server mode uses `matches.yaml` for configuration. Top-level fields:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `control_port` | int | 8080 | HTTP API port |
| `translation_model` | string | `gpt-5.4` | GPT model for translation |
| `agora_app_id` | string | from env | Overrides `AGORA_APP_ID` env var |
| `agora_app_cert` | string | from env | Overrides `AGORA_APP_CERT` env var |

Per-match fields under `matches:`:

| Field | Type | Required | Purpose |
|---|---|---|---|
| `match_id` | string | yes | Unique identifier, used in channel names |
| `sport_event_id` | string | no | Sportradar event ID for roster fetch |
| `audio` | string | yes | Path to commentary audio file |
| `video_h264` | string | yes | Path to H.264 video file |
| `events` | string | yes | Path to events file |
| `atmosphere` | string | no | Path to atmosphere WAV |
| `video_delay` | float | 7.0 | Video delay in seconds |
| `events_offset` | int | 0 | Match-time offset for events |
| `max_stt_duration` | float | 5.0 | Force-split threshold in seconds |
| `languages` | list | `[es, pt, fr, tr, de]` | Target languages |

File paths are resolved relative to the YAML file's directory (not the working directory).

## Audio Format

All PCM audio in the system is:
- 16-bit signed little-endian (S16LE)
- 16 kHz sample rate
- Mono (1 channel)
- Chunked into 10ms frames (320 bytes per chunk)

Derived constants: 32,000 bytes/second, 1,920,000 bytes/minute.

## Supported Languages

| Code | Language | Voice ID | Notes |
|---|---|---|---|
| `es` | Spanish | `jdSy6qWNc1T4C8czPgat` | Latin American accent |
| `fr` | French | `LcKoSBj8CeBInl4bQHtq` | |
| `de` | German | `g8JjujAzgjLre020BW2u` | |
| `pt` | Portuguese | `HR2TRGmi4QbMsO5omv7l` | Brazilian |
| `it` | Italian | default | |
| `ar` | Arabic | default | |
| `ja` | Japanese | default | |
| `ko` | Korean | default | |
| `zh` | Chinese | `ImsA1Fn5TNc843fFdz99` | |
| `hi` | Hindi | `LcKoSBj8CeBInl4bQHtq` | Shares French voice |
| `tr` | Turkish | `ImsA1Fn5TNc843fFdz99` | |
| `en` | English | `gU0LNdkMOQCOrPrwtbee` | Passthrough (no translation) |

Languages showing "default" use `DEFAULT_VOICE_ID` (`ImsA1Fn5TNc843fFdz99`).

## Logging Prefixes

| Prefix | Source |
|---|---|
| `[TTS #N]` | TTSEngine utterance processing (N = utterance counter) |
| `[PIPE]` | Pipe writer thread (audio delivery to Go publisher) |
| `[STT]` | Deepgram transcription results |
| `[SR]` | Sportradar event playback |
| `[DROP Xs]` | STT utterance dropped — format: `DROPPED {ms}ms — {late}s past play_at (xlat={x}s, tts={t}s, queued_behind={q}s, pre_xlat={hit\|miss})` |
| `[ATMOS]` | Atmosphere audio loading and toggle |
| `[ORIG]` | Original audio pass-through |
| `[HTTP]` | Production server HTTP API |
| `[WORKER]` | MatchWorker lifecycle events |
| `[ORCH]` | Orchestrator match start/stop |
| `[MATCH {id}]` | Per-match log lines (roster fetch, errors) |
| `[TELEMETRY]` | TTSEngine telemetry callbacks (interruption warnings) |

## Structured Match Logs

Server mode writes structured JSONL logs per match run:

```text
match_data/{match_id}/runs/{YYYYMMDD_HHMMSS}/
  stt.jsonl
  es.jsonl
  pt.jsonl
  ...
```

Conventions:

- First line in each file is a JSON object with `"type": "header"`
- Subsequent lines are one JSON object per utterance with `"type": "utterance"`
- Files are opened line-buffered and flushed after each write
- `stt.jsonl` is shared across the match
- `{lang}.jsonl` contains both `source="stt"` and `source="sr"` playback outcomes

Language-log `status` values:

- `played` — utterance started and completed normally
- `interrupted` — playback actually started but was cut short mid-playback (only set by `_pipe_writer`)
- `dropped` — item never started playback because it was too late, TTS returned no audio, or shutdown
- `replaced` — STT item never started because a fresher STT item took over its slot
- `suppressed` — STT utterance was discarded because SR was already occupying the slot

Additional telemetry fields per utterance:

- `pre_translated` — `true` if translation was served from the pre-translation cache, `false` if inline
- `queue_wait_ms` — milliseconds the item waited in the queue before the TTS worker started processing it
- `total_buffered_ms` — total TTS audio duration in milliseconds
- `speed` / `local_speed_factor` — local ffmpeg `atempo` factor applied to fit before the next STT item; `1.0` means no local speed-up
- `fit_from_ms`, `fit_to_ms`, `fit_deadline_ms`, `fit_cpu_ms` — source duration, fitted duration, available playback window, and ffmpeg processing cost for local speed fitting

Items that never played are `dropped`, `replaced`, or `suppressed`, not `interrupted`. Only `_pipe_writer` can emit `interrupted` — it detects `_interrupt.is_set()` during active chunk drain.

Telemetry counters in `LangTelemetry` follow these rules:

- `stt_played` / `sr_played` count `played` and `interrupted`
- `drop_count` counts `dropped`, `replaced`, and `suppressed`
- `stt_cut_short_count` is allowed when a fresher STT utterance interrupts older STT
- `sr_cut_short_count` is expected when STT preempts SR
- separate `stt_interrupted`, `stt_dropped`, `stt_replaced`, and `stt_suppressed` counters expose the STT outcome mix in status responses

## Related Deep Dives

- [TTS Timeline Format](L2/tts_timeline_format.md) — log analysis format for verifying audio timing

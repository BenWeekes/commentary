# L1 — Conventions

> Naming rules, audio format constants, event file syntax, and runtime behaviours like JIT translation.

## Naming

- Python files: `snake_case.py`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Classes: `PascalCase` (e.g., `TTSEngine`, `ControlHandler`)
- Go files: `snake_case.go` in `reference/`, `main.go` at root
- Events files: descriptive names with underscores (e.g., `bmg_fch_md28_full_match.txt`)

## Voice IDs

Per-language ElevenLabs voice mapping in `live_match.py` (`LANG_VOICES` dict). Languages without an entry fall back to `DEFAULT_VOICE_ID`.

Voice selection is just-in-time: `voice_for_lang()` is called at TTS time, not at queue time, so language changes take effect on the next utterance.

## Pass Filtering

Simple pass events like "to Diks." or "Elvedi to Nicolas." can be identified by the regex `_PASS_RE` and the helper `_is_simple_pass()`. These are defined but not currently active — no filtering is applied at runtime. All events are passed through regardless.

## JIT Translation

Translation is deferred to TTS time, not queue time. This means:
- The `speak()` method accepts a `translate_fn` callback
- The TTS worker calls `translate_fn(text)` just before TTS generation
- Language changes via `/set-lang` take effect on the very next utterance
- The translate function returns `(translated_text, voice_id)` tuple

## Deterministic Corrections

The `CORRECTIONS` list in `live_match.py` (~42 entries) fixes systematic Deepgram misrecognitions:
- Team names: "Flag back" → "Gladbach", "Saks Paoli" → "St. Pauli"
- Player names: "Ubijzivzivadze" → "Budu Zivzivadze"
- Commentary phrases: "in the lead." → "in the league."

Corrections are applied as simple string replacements in order. Each correction is a `(wrong, right)` tuple.

## Events File Format

```
# Comments start with #
offset_seconds|PRIORITY|message text
```

- `offset_seconds`: integer or `mm:ss` format
- `PRIORITY`: `INTERRUPT` (high priority, clears queue) or `APPEND` (normal)
- `message text`: English commentary text

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
| `[DROP Xs]` | STT utterance dropped (exceeded latency budget by X seconds) |
| `[ATMOS]` | Atmosphere audio loading and toggle |
| `[ORIG]` | Original audio pass-through |

## Related Deep Dives

- [TTS Timeline Format](L2/tts_timeline_format.md) — log analysis format for verifying audio timing

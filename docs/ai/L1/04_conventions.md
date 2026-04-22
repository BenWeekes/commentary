# L1 — Conventions

## Naming

- Python files: `snake_case.py`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Classes: `PascalCase` (e.g., `TTSEngine`, `ControlHandler`)
- Go files: `snake_case.go` in `reference/`, `main.go` at root
- Events files: descriptive names with underscores (e.g., `bmg_fch_md28_full_match.txt`)

## Voice IDs

Per-language ElevenLabs voice mapping in `live_match.py`:

| Language | Voice ID | Notes |
|---|---|---|
| Spanish | `jdSy6qWNc1T4C8czPgat` | Latin American accent |
| German | `g8JjujAzgjLre020BW2u` | |
| Default (all others) | `ImsA1Fn5TNc843fFdz99` | Fallback voice |

Voice selection is just-in-time: `voice_for_lang()` is called at TTS time, not at queue time, so language changes take effect on the next utterance.

## Pass Filtering

Simple pass events like "to Diks." or "Elvedi to Nicolas." are filtered to avoid overwhelming the listener. The regex `_PASS_RE` matches these patterns. Only 1 in 5 simple passes are kept (`pass_count % 5 != 0` → skip). All `INTERRUPT` events are always kept.

## JIT Translation

Translation is deferred to TTS time, not queue time. This means:
- The `speak()` method accepts a `translate_fn` callback
- The TTS worker calls `translate_fn(text)` just before TTS generation
- Language changes via `/set-lang` take effect on the very next utterance
- The translate function returns `(translated_text, voice_id)` tuple

## Deterministic Corrections

The `CORRECTIONS` list in `live_match.py` (~40 entries) fixes systematic Deepgram misrecognitions:
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

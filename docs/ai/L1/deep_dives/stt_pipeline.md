# L2 — STT Pipeline

## Overview

The STT pipeline (`live_match.py:791–876`) streams audio through Deepgram, applies corrections, translates, and feeds the TTSEngine.

## Pipeline Stages

```
Audio file ──▶ ffmpeg ──▶ PCM WAV ──▶ Deepgram WebSocket ──▶ apply_corrections() ──▶ translate_text() ──▶ tts.speak()
              (convert)   (16kHz)       (Nova-3)               (deterministic)        (GPT-4o-mini)       (ElevenLabs)
              ~instant    mono S16LE    ~0.8s latency          <1ms                   ~0.8s               ~0.5s
```

## Audio Feed Thread

`pcm_chunks_realtime()` reads the WAV file and yields 100ms chunks at real-time pace:

```python
bytes_per_sec = 32000  # 16kHz * 2 bytes * 1 channel
chunk_bytes = 3200     # 100ms at 32000 bytes/sec
```

The feed thread calls `ws.send_media(chunk)` for each chunk, then `ws.send_close_stream()` at EOF.

## Deepgram Configuration

```python
model="nova-3"
language="en"
encoding="linear16"
sample_rate=16000
punctuate="true"
smart_format="true"
interim_results="true"
keyterm=TERMS_LIST  # ~80 keyword terms for player/team names
```

- `interim_results=true`: Deepgram sends partial results, but only `is_final=True` results are processed
- `keyterm`: Boosts recognition of specific words (player names, team names)
- `smart_format`: Adds punctuation and formatting

## Correction System

`apply_corrections()` runs the `CORRECTIONS` list in order — each is a simple `str.replace()`:

```python
CORRECTIONS = [
    ("Flag back", "Gladbach"),
    ("Saks Paoli", "St. Pauli"),
    ("Ubijzivzivadze", "Budu Zivzivadze"),
    ...
]
```

Corrections must be ordered carefully — longer phrases should come before shorter substrings. For example, "Flag back all in white" → "Gladbach all in white" must appear before "Flag back" → "Gladbach".

## Latency Budget

The STT pipeline tracks three latency components:

| Component | Measurement |
|---|---|
| STT latency | `wall_now - audio_end` (wall clock time minus audio timestamp) |
| Translation latency | `time.time()` around `translate_text()` call |
| Total latency | `(time.time() - wall_start) - audio_end` |

If total latency exceeds `MAX_LATENCY_S` (3.5s), the utterance is dropped:

```python
if total_latency > MAX_LATENCY_S:
    print(f"  [DROP {total_latency:.1f}s] {corrected[:40]}")
    continue
```

## Benchmark Script

`stt_realtime_translate.py` measures the same pipeline with detailed per-utterance stats:

- Per-utterance: STT latency, translation latency, total latency
- Aggregate: mean, median, P90, P95, min, max
- 3-second budget analysis: percentage of utterances within 1.5s, 2.0s, 3.0s
- Saves results to JSON and text files

## Language at Translation Time

In `live_match.py`, the current language is read from a file (`/tmp/sportradar_lang`) at translation time, not at queue time. This allows the viewer to change the language and have it take effect on the next utterance:

```python
cur_lang = get_current_lang(lang_file, lang) if lang_file else lang
cur_voice = voice_for_lang(cur_lang)
if cur_lang != "en":
    translated = translate_text(oai_client, corrected, cur_lang)
```

## Deepgram vs Soniox

The repo originally tested both Deepgram and Soniox STT. Only Deepgram is used in the final system. `soniox_realtime_stt.py` and `soniox_examples/` are excluded from this repo.

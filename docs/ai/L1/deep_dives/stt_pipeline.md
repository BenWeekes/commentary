# L2 — STT Pipeline

## Overview

The STT pipeline streams live game audio through Deepgram, corrects player/team names, translates to the viewer's language, and schedules TTS playback at the original commentary timing.

## Pipeline Stages

```
Audio ──▶ ffmpeg ──▶ PCM ──▶ Deepgram ──▶ corrections ──▶ tts.speak(play_at=...)
          (16kHz mono)       (Nova-3)      (str.replace)   → translate + TTS in worker
```

## play_at Scheduling

The Go publisher delays video by `--video-delay` seconds while the STT pipeline processes audio immediately. This gives the pipeline a head start. Each Deepgram result includes `audio_start` — when the commentator spoke in the original audio:

```python
play_at = video_start + audio_start
```

`video_start` is set when the Go publisher finishes its delay and starts sending frames. Since the audio feed began `video_delay` seconds earlier, translations are already ready when the viewer sees each moment.

The TTS worker holds the audio until `play_at`, then plays. If translate+TTS takes too long and `play_at` has passed by >100ms, the utterance is dropped.

## Deepgram Configuration

```python
model="nova-3", language="en", encoding="linear16", sample_rate=16000,
punctuate="true", smart_format="true", interim_results="true",
endpointing="200", utterance_end_ms="1000", keyterm=TERMS_LIST
```

- `endpointing=200`: Deepgram fires speech_final after 200ms silence (faster turns)
- `utterance_end_ms=1000`: Minimum allowed by Deepgram API
- Only `is_final=True` results are processed (interims skipped)
- `keyterm`: ~80 player/team names for recognition boost

## Latency Budget

With `--video-delay N` (default 7s):

```
Budget per utterance ≈ N - utterance_duration - ~1.0s (translate + TTS fetch)

Example: 3s utterance, 7s delay → 7 - 3 - 1.0 = 3.0s margin (comfortable)
Example: 5s utterance, 7s delay → 7 - 5 - 1.0 = 1.0s margin (ok)
Example: 5s utterance, 6s delay → 6 - 5 - 1.0 = 0.0s margin (drops likely)
```

At 7s delay, ~12/14 utterances play on time (1 suppressed during GOAL, occasionally 1 long utterance drops).

## Correction System

`apply_corrections()` fixes common Deepgram misrecognitions:

```python
CORRECTIONS = [("Flag back", "Gladbach"), ("Saks Paoli", "St. Pauli"), ...]
```

Longer phrases before shorter substrings. Applied before translation.

## Language Switching

Language is read from a per-session file at translation time (not queue time), so language changes take effect on the next utterance.

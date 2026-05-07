# L2 — STT Pipeline

> **When to Read This:** You are modifying the Deepgram integration, adding or editing STT corrections, changing the forced split logic, or debugging translation latency.

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

The TTS worker holds the audio until `play_at`, then plays at the exact scheduled time. If translate+TTS takes too long and `play_at` has already passed, the utterance is dropped.

## Deepgram Configuration

```python
model="nova-3", language="en", encoding="linear16", sample_rate=16000,
punctuate="true", smart_format="true", interim_results="true",
endpointing="200", utterance_end_ms="1000", keyterm=TERMS_LIST
```

- `endpointing=200`: Deepgram fires speech_final after 200ms silence (faster turns)
- `utterance_end_ms=1000`: Minimum allowed by Deepgram API
- `is_final=True` results are processed; interims are monitored for forced splitting
- `keyterm`: ~80 player/team names for recognition boost

## Latency Budget

With `--video-delay N` (default 8s):

```
Budget per utterance ≈ N - utterance_duration - ~1.5s (translate + TTS fetch)

Example: 3s utterance, 8s delay → 8 - 3 - 1.5 = 3.5s margin (comfortable)
Example: 5s utterance, 8s delay → 8 - 5 - 1.5 = 1.5s margin (ok)
Example: 7s utterance, 8s delay → 8 - 7 - 1.5 = -0.5s margin (drops likely)
```

The TTS worker uses **lookahead**: while the current utterance plays (3-5s), the next one is already being translated and TTS'd in parallel. This means playback duration of the current item doesn't eat into the next item's budget, recovering 3-5s of margin per utterance.

## Forced Split (Long Utterance Protection)

Stadium crowd noise prevents Deepgram's VAD from detecting commentary pauses — audio levels only drop from -20 dB (speech) to -37 dB (crowd), well above Deepgram's silence threshold. This causes occasional mega-batches (6-10s) that exhaust the video delay budget.

The pipeline monitors interim results and force-splits when duration exceeds `--max-stt-duration` (default 5.0s):

```
Normal:   is_final(3.5s) → emit → is_final(4.2s) → emit
Forced:   interim(5.0s) → SPLIT emit → is_final(7.6s) → REMAINDER emit (from 5.0s onward)
```

- Split uses the interim transcript (slightly unstable but better than dropped)
- The subsequent `is_final` emits only the remainder portion with adjusted `play_at`
- One split per utterance — enough to bring 9.4s worst-case batches into budget
- At 10s delay with forced split: zero drops across 5 languages

## Correction System

`apply_corrections()` fixes common Deepgram misrecognitions:

```python
CORRECTIONS = [("Flag back", "Gladbach"), ("Saks Paoli", "St. Pauli"), ...]
```

Longer phrases before shorter substrings. Applied before translation.

## Language Switching

Language is read from a per-session file at translation time (not queue time), so language changes take effect on the next utterance.

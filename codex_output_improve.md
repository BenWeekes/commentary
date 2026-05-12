# Live Demo Output Validation Plan

This plan is about producing reviewable translated-video artifacts, not about standalone STT scoring. The goal is to run the 24/25 minute Mainz vs Union evaluation clip through the same local SRT -> live ingest -> translation -> Agora -> cloud recording path used for live matches.

## Goal

Generate cloud recordings for each translated channel so reviewers can judge the actual viewer experience:

- video/audio sync
- translation quality
- sentence completeness
- commentator/expert separation
- TTS naturalness
- drops/interruption behavior
- atmosphere/video alignment
- end-to-end stability through the live SRT path

This should produce shareable HLS URLs per language, comparable to real match recordings.

## Why This Matters

Standalone STT evaluation answers: "Which provider/settings produce the best English turns?"

Live-demo cloud recordings answer: "Does the whole product sound and look good when run through the production-like live path?"

The latter exercises:

- local SRT looper
- `srt_direct` ingest
- commentary PCM extraction
- video delay scheduling
- atmosphere delay/mixing when present
- STT provider integration
- translation
- TTS generation and speed fitting
- per-language Agora publishing
- cloud recording
- final HLS playback

## Source Clip

Use the reviewed evaluation section:

- Match/run reference: `m05_uni_md33`, `20260510_190915`
- Live-style source clip: `clips/m05_uni_eval_25min/source.mp4`
- Gold transcript: `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json`
- Public gold review page: `https://sip.dev.gw.01.agora.io/stt_eval_m05_uni_md33.html`

The clip already represents the section colleagues can review against the gold transcript.

## Proposed Run Matrix

Keep each recording run to one exact configuration. Do not sweep providers/settings inside a single recorded run.

Initial run:

| Field | Value |
|---|---|
| Source mode | `demo_srt_direct` |
| STT provider | Soniox realtime `stt-rt-v4` |
| STT endpoint | `max_endpoint_delay_ms=1000` |
| Video delay | `14s` |
| Translation | current GPT-5.4 path |
| TTS | current ElevenLabs path with local speed-fit |
| Name correction | disabled unless implemented as raw+corrected logging |
| Channels | original + all configured translated languages |
| Recording | cloud recording enabled for every translated channel, and ideally original too |

Second run only if needed:

| Change | Reason |
|---|---|
| Video delay `16s` | If 14s still causes drops or rushed TTS for slower voices/languages |
| Soniox endpoint `700ms` | If reviewers prefer lower latency and can tolerate shorter turns |
| Soniox endpoint `1500ms` | Only if human review shows 1000ms still cuts too aggressively |

## Required Metadata To Capture

Every recording run should log enough config to make review meaningful:

- run id
- source clip path
- source mode and local SRT port
- STT provider and model
- STT endpoint delay
- keyterms file/count
- raw STT vs corrected STT setting
- translation model
- translation prompt version
- TTS provider/model
- voice IDs per language and speaker, if speaker voices are enabled
- ElevenLabs stability/similarity settings
- local speed-fit settings
- video delay
- cloud recording resource/sid/channel/UID per language
- final HLS URL per language

## Expected Outputs

For each language:

- `{lang}.jsonl` language log
- `stt.jsonl` shared STT log
- `recordings.json`
- cloud recording HLS URL
- match detail URL pinned to the run

Suggested review table:

| Lang | Channel | HLS URL | Detail URL | Notes |
|---|---|---|---|---|
| original | `{match}-original` | ... | ... | Sync reference |
| es | `{match}-es` | ... | ... | |
| pt | `{match}-pt` | ... | ... | |
| fr | `{match}-fr` | ... | ... | |
| tr | `{match}-tr` | ... | ... | |
| de | `{match}-de` | ... | ... | |

## Validation Checklist

Before starting:

- Confirm no other process owns the local SRT demo port.
- Confirm server is using the intended config.
- Confirm cloud recording is enabled and set to audio+video.
- Confirm media publisher tokens are 24h.
- Confirm `m05_uni_eval_demo` or equivalent demo-live row points to the right source clip.
- Confirm the run will not overwrite or confuse previous recording metadata.

During run:

- Watch status page for all languages running.
- Confirm STT utterances are arriving.
- Confirm TTS playback is occurring in language logs.
- Confirm `intended_skew_ms` remains near zero.
- Confirm cloud recording started for each channel.
- Confirm no unexpected SRT reconnects, no publisher crashes, no silent channels.

After run:

- Confirm cloud recording stopped/flushed cleanly.
- Extract HLS URLs from `recordings.json`.
- Check HLS duration is roughly the clip duration plus expected startup/teardown margin.
- Spot-check original channel sync.
- Spot-check each translated channel for:
  - translated audio present
  - no long initial silence beyond expected delay/startup
  - video present
  - atmosphere/video sync if atmosphere is included
  - no double voices unless intended
  - no excessive drops

## Review Questions For Humans

Ask reviewers to focus on:

- Does the translated commentary describe the moment currently visible on screen?
- Are sentences complete enough to understand?
- Are there unnatural pauses within sentences?
- Are football names and terms correct?
- Are commentator and expert turns handled naturally?
- Does the TTS voice sound natural at the speed used?
- Is the atmosphere bed in sync with video?
- Are there specific timestamps where meaning is wrong or audio is missing?

## Relation To STT Experiment

Use `codex_stt_improve.md` for provider/scoring methodology.

This recording run should use one selected STT candidate. If the output sounds bad, diagnose by mapping the issue back to:

- STT recognition
- STT turn boundary
- speaker diarization
- name correction
- translation
- TTS generation/speed-fit
- playback scheduling
- Agora/cloud recording

Do not change multiple variables at once between recording runs.

## Open Implementation Questions

- Should original also be cloud-recorded for sync comparison?
- Should the demo clip include atmosphere separately, or is embedded program audio enough for this review?
- Should name correction be enabled for this first output recording, or held back until raw-vs-corrected logging is implemented?
- Should speaker-specific voices be enabled now, or wait until diarization stability is validated?
- Should 14s and 16s delay runs both be recorded for direct comparison?

## Suggested Next Step

Once STT validation agrees on a candidate, run one `m05_uni_eval_demo` cloud-recorded live-demo pass with:

- Soniox `stt-rt-v4`
- `max_endpoint_delay_ms=1000`
- 14s video delay
- current GPT-5.4 translation
- current ElevenLabs TTS/speed-fit

Then publish the HLS URLs and run detail URL for human review.

# SRT H264 / AAC Notes

## Priority

The real goal is:

- pull SRT once
- keep **video as direct H.264 passthrough** if possible
- decode **audio to PCM only because STT needs it**

So this note should be read with that priority in mind:

- **H.264 passthrough is the target**
- **AAC handling is secondary**

## Current SRT source shape

The direct SRT source we probed at:

`srt://185.188.55.51:33999?streamid=...`

contained:

- video: `H.264`, `1920x1080`, progressive, `25 fps`
- audio track 1: `AAC-LC`, `48 kHz`, stereo
- audio track 2: `AAC-LC`, `48 kHz`, stereo

Important detail:

- the two AAC tracks were extracted and compared
- they were byte-for-byte identical
- so this was **not** separate commentary + atmosphere
- it was effectively one duplicated program-audio feed

## What is the problem with sending the AAC directly?

The main issue is not that AAC is impossible in principle. The issue is that direct AAC passthrough does not solve the actual pipeline needs, and it is not the part we most care about optimizing.

### 1. STT still needs PCM

Our speech-to-text path wants decoded PCM. Even if we preserve AAC for republishing, we still need to decode that same AAC to PCM for:

- live transcription
- translation timing
- TTS scheduling

So AAC passthrough would still require a parallel decode path.

### 2. The SRT feed did not give us separate audio semantics

The SRT source we tested did **not** expose:

- commentary on one track
- atmosphere on another track

Instead it exposed duplicated program audio. That means direct AAC forwarding would only preserve that combined program feed. It would not restore the current Agora-source model of:

- commentary UID
- atmosphere UID

### 3. Our proven working path already uses PCM

For the live fallback that actually worked in browser, we used:

- SRT in
- video decoded to YUV
- audio decoded to PCM
- publish into Agora

That path is proven. Browser audio was present once playback was started by user interaction.

### 4. Direct AAC forwarding has low payoff right now

Even if we made AAC passthrough work cleanly from the SRT transport, we would still need:

- PCM for STT
- logic to decide what the republished audio should be
- handling for the fact that the current SRT source is just combined program audio

So preserving AAC directly does not materially simplify the system for the current live use case.

## Practical conclusion

For the current direct-SRT live path, the right approach is:

- keep pulling the SRT feed once
- decode audio to PCM
- use that PCM for STT
- publish PCM into the internal Agora source channel

That is the safest and simplest path because it matches the downstream speech pipeline and it is already working.

## What is actually blocking encoded mode?

The hard problem is **video**, not AAC audio.

What worked:

- YUV video + PCM audio

What did not work yet:

- direct H.264 passthrough from the SRT-derived stream into Agora encoded publish
- several re-encoded H.264 variants pushed through the encoded path

So the current state is:

- **AAC is not the blocker**
- **direct encoded H.264 handoff is the blocker**
- **PCM audio is just the STT requirement**

## Desired end state

The preferred production shape is:

- SRT pull
- H.264 video passed through directly into Agora encoded publish
- AAC decoded to PCM for STT
- republish one shared internal Agora source channel for all translations

That avoids video transcode cost while still giving STT the audio format it needs.

## Recommended next-step stance

For live rollout right now:

- use direct SRT pull
- decode audio to PCM
- decode video to YUV
- publish one shared internal Agora source channel

Later optimization:

- revisit encoded H.264 publish once the exact Agora encoded-frame contract is understood
- only revisit AAC passthrough if there is a concrete need to preserve source audio without PCM republish

## Clear summary

What we want:

- **publish the H.264 coming from SRT directly if possible**

What we do not care much about:

- preserving AAC just for its own sake

What we still need regardless:

- PCM audio for STT

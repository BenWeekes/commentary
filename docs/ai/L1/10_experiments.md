# Side Experiments

> Reference for things we have **tried** alongside the production pipeline.
> Each entry: what we tested, what we found, where the artefacts live.
>
> Experiment **results live in `experiments/`** (gitignored — local to this machine).
> This page is the index so a fresh AI context can find them.

## Convention

- `experiments/<name>/` holds scripts + small text artefacts (transcripts, JSONL event logs, comparison reports, sample WAVs).
- Large binaries (raw PCM extracts, full MP4 muxes) are **not** preserved here — they regenerate from `clips/m05_uni_eval_25min/source.mp4` and the scripts in the experiment folder.
- If the same experiment is re-run, **either** create a new subfolder named with a timestamp, **or** overwrite — pick whichever is appropriate for the comparison being kept.

---

## 1. V2V provider comparison (Gemini Live vs Soniox v5, 5-minute slice)

### Status

Negative on Gemini Live for continuous football commentary. Positive signal on Soniox v5 with built-in translation as a possible 1-stage replacement for current Soniox v4 + gpt-5.5 + ElevenLabs.

### Source clip

5-minute slice `300 s – 600 s` (= 5:00 to 10:00) of `clips/m05_uni_eval_25min/source.mp4`. Real Mainz vs Union Berlin commentary, 562 gold words. Gold transcript: `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/`.

### Headline result

| Provider | EN coverage | WER vs gold | FR coverage |
|---|---|---|---|
| Soniox v4 (current production) | 100.7 % | **5.9 %** | n/a (translated via gpt-5.5 + ElevenLabs) |
| Soniox v5 + built-in translation | 100.9 % | 6.8 % | **full 5 min, 672 FR words** |
| Gemini Live `gemini-3.1-flash-live-preview` (default) | 24.4 % | 80.8 % | only ~57 s of FR audio |
| Gemini Live with `NO_INTERRUPTION` config | 26.2 % | 81.1 % | similar |

**Key findings:**
- Gemini Live: drops ~75 % of input on continuous dense commentary regardless of turn-management config; not viable for football today.
- Soniox v5: STT roughly tied with v4; built-in real-time translation works and covers the full source; ~1-1.5 s end-to-end latency improvement over current 3-stage pipeline.
- v5 translation propagates STT errors literally (e.g. `Freak it has been given` → `On a vraiment tout donné`). Mitigation likely via v5's `translation_terms` config field, untested.
- Gemini Live offers no per-token timestamps; v5 retains them.

### Artefacts

| File | Contents |
|---|---|
| `experiments/v2v_5min_slice/comparison_report.md` | Full write-up, three-way comparison table, listening-link references |
| `experiments/v2v_5min_slice/gold_en_300_600.txt` | Ground truth (gold transcript) for the window |
| `experiments/v2v_5min_slice/soniox_en_300_600.txt` | Soniox v4 reference transcript (from run `20260514_154918`) |
| `experiments/v2v_5min_slice/soniox_v5_en.txt`, `.txt`-fr, `_tokens.jsonl` | Soniox v5 EN + FR text + raw token stream with start_ms/end_ms |
| `experiments/v2v_5min_slice/gemini_events.jsonl`, `gemini_no_interrupt_events.jsonl` | Full Gemini Live event stream per variant (timestamps, transcripts, audio chunks) |
| `experiments/v2v_5min_slice/gemini_en_full.txt`, `gemini_fr_full.txt` | Gemini concatenated transcripts |
| `experiments/v2v_5min_slice/gemini_fr_audio.wav`, `gemini_no_interrupt_fr_audio.wav` | The 57 s of Gemini French output (24 kHz mono) |
| `experiments/v2v_5min_slice/wer_report.txt` | WER summary numbers |
| `experiments/v2v_5min_slice/run_gemini.py`, `run_gemini_variant.py`, `run_soniox_v5.py` | The probe scripts. Re-runnable. |
| `experiments/v2v_5min_slice/score_wer.py` | WER scorer (concatenate-and-Levenshtein on normalised text) |
| `experiments/v2v_5min_slice/make_mp4.sh` | Mux script (regenerates the side-by-side MP4 from source + Gemini audio) |

### Listening links (server-side nginx, may be cleaned up at any time)

- http://sip.dev.gw.01.agora.io/v2v_source_5min.mp4 — original English audio, full 5 min
- http://sip.dev.gw.01.agora.io/v2v_gemini_only.mp4 — only the 57 s Gemini produced (video truncated)
- http://sip.dev.gw.01.agora.io/v2v_gemini_5min.mp4 — 5 min, English on left channel, Gemini French on right
- http://sip.dev.gw.01.agora.io/v2v_gemini_comparison.md — comparison report served as static markdown

### Re-run instructions

Pre-requisites: `GEMINI_API_KEY` env var (key was AI-Studio free tier; daily quota will rate-limit parallel runs), `SONIOX_API_KEY` at `/home/ubuntu/soniox`.

```bash
# 1. Regenerate the 5-min slice if /tmp/v2v_compare/ has been cleaned
mkdir -p /tmp/v2v_compare && cd /tmp/v2v_compare
ffmpeg -y -ss 300 -i /home/ubuntu/commentary/clips/m05_uni_eval_25min/source.mp4 -t 300 \
    -map 0:v:0 -map 0:a:0 -c:v copy -c:a copy slice_5min.mp4
ffmpeg -y -i slice_5min.mp4 -ar 16000 -ac 1 -f s16le slice_5min.pcm

# 2. Run probes from the experiment folder
cd /home/ubuntu/commentary/experiments/v2v_5min_slice
GEMINI_API_KEY=<key> /home/ubuntu/commentary/.venv/bin/python run_gemini.py
GEMINI_API_KEY=<key> VARIANT=no_interrupt /home/ubuntu/commentary/.venv/bin/python run_gemini_variant.py
/home/ubuntu/commentary/.venv/bin/python run_soniox_v5.py

# 3. Score
/home/ubuntu/commentary/.venv/bin/python score_wer.py
```

### Open follow-ups

1. Test Soniox v5 `translation_terms` to confirm it can serve as the equivalent of `lib/corrections.py`.
2. Run all 5 target languages (es/pt/fr/tr/de) through v5 — does PT come back with similar length characteristics, or does v5 fix the PT-cadence problem?
3. If translation quality holds up, scope a migration from Soniox v4 + gpt-5.5 to Soniox v5 single call.
4. Re-test Gemini if a key for `gemini-3.1-flash-lite-live-translate` (closed-beta translate-specific model) becomes available — it may behave differently from the conversational `gemini-3.1-flash-live-preview` we tested. **✅ DONE 2026-07-07 — see update below.**

### Update (2026-07-07): translate-specific model reverses the negative result

Re-ran with **`gemini-3.1-flash-lite-live-translate`** ("Gemini v4xs Translation S2ST WizLive", the purpose-built streaming speech-to-speech translation model — now available on our key). Setup: `translationConfig.targetLanguageCode="fr"` in `generationConfig`; `inputAudioTranscription`/`outputAudioTranscription` at setup level; `responseModalities:["AUDIO"]` (text-only not allowed — audio-restricted for latency). Runner: `experiments/v2v_5min_slice/run_gemini_stt.py`.

**The coverage problem is gone.** On the same 5-min slice:

| Metric | conversational `flash-live-preview` (old) | **`flash-lite-live-translate` (new)** | Soniox v4 |
|---|---:|---:|---:|
| EN STT coverage | 24 % | **104 %** (586 vs 562 gold words) | 100 % |
| EN WER vs gold | 80.8 % | **22.1 %** | 5.9 % |
| Interruptions (dropped turns) | many | **0** | — |
| Tail latency (last transcript vs audio end) | — | **−0.5 s (real-time)** | higher |
| Output in one stream | — | EN text + FR text + 317 s FR audio | STT only |

- The turn-management input-dropping that killed the conversational model does NOT happen on the translate model. Full coverage, zero interruptions, real-time.
- WER 22.1 % is almost entirely **proper nouns / football terms** (Mainz→"might", Khedira→"Kera", "free kick"→"three kick") — a small known vocabulary fixable with a roster-glossary correction pass (cf. `lib/corrections.py`).
- Delivers STT + FR translation (text **and** audio) in a single low-latency stream — a candidate one-call replacement for the Soniox → gpt-5.5 → ElevenLabs chain when latency dominates and ~22 % WER (pre-correction) is acceptable.

Open follow-ups: (a) roster-glossary correction pass to pull WER toward Soniox; (b) test the other 4 target languages (es/pt/tr/de); (c) precise end-to-end latency A/B vs the Soniox pipeline.

---

## 2. AI commentator from video frames (vision-LLM commentary)

### Status

Active. v4 served behind nginx, 3-column comparison page live. **Goal:** produce a commentary track from the video stream alone (no STT) that approaches the cadence, accuracy and style of the real broadcast booth. End-state would let us cover matches where no English commentary track exists, or generate alternate-language commentary directly from the world feed.

### Source clip

Same 5-min slice as the V2V comparison above: `300–600 s` of `clips/m05_uni_eval_25min/source.mp4` (Mainz vs Union Berlin MD33). Reference: Soniox gold-corrected STT at `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/`.

### Architecture (per-burst, gpt-5.4-mini)

```
4 frames (0.55 s apart) ──► [gpt-5.4-mini vision]
+ static match context             │
+ recent N accepted lines          ├─► commentary line (3-12 words)
+ team alias usage in last 3 lines │   or NO_CALL
+ pre-game player insights         │
+ team alias bank                  │
                                   │
                                   ▼
                            [emotion-tag pick — gpt-5.4]
                                   │
                                   ▼
                            [eleven_v3 TTS, voice = sportscaster]
                                   │
                                   ▼
                            scheduled at video_time + 0.3 s
```

Each vision call is essentially stateless across calls (text-only memory of recent lines). Inside a burst it sees 2.2 s of action and can read motion. The architecture is described in more detail in the experiment folder's README.

### Origin, upstream, and the v1 prompt

The whole line of work started from the upstream project
**[zicojiao/worldcupvoice](https://github.com/zicojiao/worldcupvoice)** — the same
core loop (frame-burst sampling → vision LLM → ElevenLabs TTS → publish audio),
originally built as a broadcast-to-Agora relay. This experiment fanned out from
there into: multi-language TTS, live SRT ingest, judge-based scoring, cross-frame
temporal context, and the two-stage safe-draft/polisher split.

**The v1 prompt — the one that started everything** — lives in
`experiments/ai_commentator/run_ai_commentator.py` (`build_visual_prompt` +
`STYLE_PROMPT`). Its seed idea, preserved *verbatim* in every version since (still
the first line of `V5_PROMPT_BODY` and the `run_gpt55_variant` prompts):

> "You are a live football play-by-play commentator, not an image captioner."

v1 already established the **number-first naming rule**: inspect the visible shirt
numbers on the ball carrier / passer / crosser / shooter / goalkeeper / nearest
defender; use the roster short-name only if the number is readable and the kit
matches, otherwise fall back to a generic role ("the Mainz striker"). This is the
ancestor of both the later "generic over incorrect" framing and the current
direction of **outputting shirt numbers + positions and substituting names
downstream** rather than asking the vision model to recall names.

Lineage:
- **v1** — original prompt (`run_ai_commentator.py`), ~120 lines/5 min, every line named a team; energetic but high hallucination.
- **v2–v4** — strict naming, live-pace booth-busy gate, team-alias rotation, sub-board recognition, NO_CALL bias.
- **v5** — sub-event state memory, trigram dedup, frame carry-over → the low-latency Pareto point.
- **v18–v20** — two-stage architecture (safe-draft vision + text polisher) that structurally caps hallucination.
- **v20_par** (2026-07-06) — parallel/async vision so a slow, CPU-contention-starved call can't freeze the live loop on the 4-core + T4 box (see [[gpu-box-and-latency-fix]] / `DEBUG_latency_tail.md`).

### Iteration ladder

| Version | Key change | Lines / 5 min | "Mainz"/"Union" mentions | Scoreline | Sub-board read |
|---|---|---|---|---|---|
| v1 | dense, soft naming | ~120 | every line | 4 | no |
| v2 | strict naming, live-pace gate, style examples, pre-game player notes | 41 | every line | 8 | no |
| v3 | calibrated naming ("lean in, occasional misIDs OK"), natural sync | 60 | every line | 10 | no |
| v4 | scoreline rule, **alias rotation** (alias bank, banned-this-turn list), sub-board recognition, set-piece attribution rule, eleven_v3 + emotion tags | 77 | 8 / 2 | 0 | yes (5 subs) |
| v5 | sub-event memory, trigram dedup, frame carry-over, dynamic gate, stronger NO_CALL | 63 | 8 / 6 | 0 | yes (2 subs) |
| v6 | parametric sweep over vision model + prompt variants; **leaderboard with 14 metrics** | ~varies | varies | 0 | yes |

Real broadcaster reference (Soniox gold): 34 turns in 5 min, 8.71 s mean gap, std 7.41.

### Leaderboard — final (v4 → v12, 2026-07-01)

14 metrics computed: lines/5min, mean & std inter-line gap, trigram repetition rate, type-token ratio, alias entropy, player-name density, action-verb density, "Mainz"/"Union" mention counts, LLM-judge hallucination rate, LLM-judge human-likeness (1-5), subject-present rate, Soniox coverage. See `experiments/ai_commentator/score.py` + `judge.py`. Full results in `experiments/ai_commentator/leaderboard.json`.

**Headline result, sorted by hallucination rate (↑)** — pipeline p90 = vision + 0.3 s TTS first byte + 0.3 s natural-reaction beat:

| Variant | Vision model + prompt | Lines | Gap μ | Hallu ↓ | Human-like ↑ | Cover | Pipe p90 |
|---|---|---:|---:|---:|---:|---:|---:|
| **soniox_gold** (reference) | n/a — real broadcaster | 34 | 8.71 | — | — | — | — |
| **gpt55_playerist** ★ | gpt-5.5 + playerist | 60 | 4.98 | **8 %** | 3.88 | 100 % | 9.2 s |
| gpt55_en | gpt-5.5 baseline | 49 | 6.11 | 14 % | 3.80 | 88 % | 7.0 s |
| gpt55_quiet | gpt-5.5 + "be quiet" | 19 | 15.4 | 15 % | 4.00 | 52 % | 5.9 s |
| gpt54_en | gpt-5.4 baseline | 48 | 6.14 | 16 % | 3.88 | 82 % | 5.5 s |
| v8a | mini + Gemini + strict arbiter + rich | 50 | 6.01 | 16 % | 3.58 | 100 % | 6.4 s |
| **v5** (baseline) | gpt-5.4-mini + sub state + trigram | 63 | 4.76 | 17 % | 3.81 | 100 % | 3.1 s |
| v8d | mini + Gemini + confidence-gated | 48 | 6.33 | 19 % | 3.66 | 100 % | 5.9 s |
| v8b | mini + Gemini + gpt-5.5 arbiter | 48 | 6.25 | 20 % | 3.59 | 100 % | 8.5 s |
| v10 | gpt-5.4-mini + playerist | 66 | 4.52 | 20 % | 3.68 | 100 % | 4.0 s |
| v9 | mini + Gemini + strict + agreement + verifier | 55 | 5.49 | 20 % | 3.33 | 100 % | 8.4 s |
| v8c | mini + Gemini + gpt-5.5 arbiter + strict | 48 | 6.21 | 21 % | 3.47 | 100 % | 8.5 s |
| v11 | gpt-5.4 + playerist | 59 | 5.04 | 22 % | 3.85 | 100 % | 5.0 s |
| v4 | gpt-5.4-mini + 5 generic rules | 77 | 3.89 | 22 % | 3.87 | 100 % | 3.4 s |
| gpt55_long | gpt-5.5 + "long sentences" | 38 | 7.95 | 26 % | 4.03 | 100 % | 8.1 s |
| v8 base | mini + Gemini + arbiter + rich context | 42 | 7.20 | 27 % | 3.66 | 97 % | 5.5 s |
| v12 | Gemini + playerist | 45 | 6.74 | 31 % | 3.33 | 97 % | 2.9 s |
| gemini_en | Gemini baseline | 43 | 6.88 | 32 % | 3.37 | 91 % | 2.9 s |

### Prompt × Model matrix (hallucination rate)

Cross-tabulating the same rich-context prompt across all vision models and the "playerist" prompt (which forces `[player] + [generic verb] + [location]` and bans explicit team-name refs):

| Vision model | baseline prompt | playerist prompt | latency (vision p90) |
|---|---:|---:|---:|
| gpt-5.4-mini | 17 % (v5) | 20 % (v10) | 2.5 s / 3.2 s |
| gpt-5.4 (full) | 16 % (gpt54_en) | 22 % (v11) | 4.9 s / 4.0 s |
| **gpt-5.5** | 14 % (gpt55_en) | **8 %** (gpt55_playerist) | 6.4 s / 8.6 s |
| Gemini 2.5 flash | 32 % (gemini_en) | 31 % (v12) | 2.3 s / 1.6 s |

**Only gpt-5.5 responds to playerist with a hallucination drop.** Smaller models produce more player-named sentences under playerist but keep speculating about events at the same rate. This says the constraint isn't "name a player" — it's "if uncertain about the event, downgrade to a description that's clearly true from the frame." That requires the reasoning capability of gpt-5.5.

### Hybrid conclusion — dropped

Running gpt-5.4-mini and Gemini in parallel per burst and merging their outputs (v7, v8, v8a, v8b, v8c, v8d, v9) was expected to reduce hallucinations by catching single-model errors. **It didn't, meaningfully.**

- v5 (single mini): 17 % hallu, 3.1 s pipe
- v8a (mini + Gemini + strict arbiter + rich context): 16 % hallu, 6.4 s pipe

**+3.3 s latency and 3× the code complexity buys 1 % hallucination reduction.** From the correlation analysis: 4 of 11 hallucinations were correlated (both models wrong the same way — hybrid can't catch these), 7 were single-model errors that the arbiter picked anyway.

**Gemini's independent contributions were:**
- Best alias variety and best player-name density in isolation
- Fastest vision (1-1.6 s), useful when latency dominates
- Zero help on hallucinations — its baseline is 32 %, playerist keeps it there

### Sweet spots at each latency budget

- **≤ 5 s pipeline (low-latency):** **v5** (gpt-5.4-mini alone) at 17 % hallu, 3.81 humanlike, 3.1 s pipe. Simplest single-model config. No Gemini.
- **≤ 10 s pipeline (higher quality):** **gpt55_playerist** at 8 % hallu, 3.88 humanlike, 9.2 s pipe. Half the hallucination rate at ~3× the latency of v5. This is the production pick when quality dominates.

Both fit their target with translation and TTS included (translation is a cheap +0.3 s text-only call, piggybacks on the arbiter/post-vision step).

### Live SRT integration + real live runs

`experiments/ai_commentator/live_srt_run.py` closes the loop: `ffmpeg -re` pushes the source clip via SRT to `srt://127.0.0.1:1008X`, a second `ffmpeg` subscribes as caller and emits 960×540 JPEGs at the configured cadence, then per-burst gpt-5.5 (or gpt-5.4-mini) vision + FR translation via gpt-5.4-mini + ElevenLabs `eleven_v3` TTS run in parallel per line. Wall-time-vs-video-time lag is measured per accepted line and rolled up to p50/p90.

Both pipelines have been run through this loop:

| Pipeline | Model + prompt | Wall-time overhead | p90 pipeline lag | Fixed broadcast delay |
|---|---|---|---|---|
| Pipeline 1 | gpt-5.5 + playerist + rich context | ~7 s (on a 300 s clip) | 6.3-7.5 s | **8 s** |
| Pipeline 2 | gpt-5.4-mini + v5 + rich context | ~5 s (on a 300 s clip) | 3.1-3.9 s | **4 s** |

`build_public_page.py` renders a public results page (`results.html`) with hidden internal names ("Pipeline 1 / Pipeline 2"), auto-snapshots each run to `/experiments/ai_commentator/YYYYMMDD-HHMM/` for stable share links, and drops an internal `notes.md` per snapshot documenting the actual model/prompt combos.

### User-feedback improvements round 2 (2026-07-02)

Post-launch feedback triggered six additions on top of the initial live pipelines:

| Feedback | Implementation |
|---|---|
| Referee always called "Exner" | `repetition_helpers.summarise_referee_usage` — same rotation mechanism as team aliases (Exner / Mr Exner / the referee / the official) |
| Waiting-lines repeat awkwardly | `is_repeated_waiting` in `repetition_helpers.py` — RE-BASED filter: if the prior line already said "X waits", the next same-subject waiting line becomes NO_CALL |
| French vocab too literal (e.g. `brèche`) | Expanded `TRANSLATE_SYSTEM` in `live_srt_run.py` with football-native French mappings (`brèche → faille/ouverture`, `wall → mur défensif`, `hosts → les locaux`, etc.) |
| No crowd atmosphere | `mux_with_crowd.py` — extracts the source audio, applies a high-pass to reduce speech energy, attenuates to −22 dB and mixes under the AI commentary at 0 dB. Both pipelines. |
| Detect slow-mo replays | Added a `REPLAY: yes|no` prefix protocol to the vision prompt. On yes, the runner overrides the line with a rotating canned phrase ("And here's the replay of that moment.", etc.) |
| Reported audio-delay perception | Investigated — no scheduling overlaps found. Kept 0.3 s natural-reaction beat. Likely playback-device latency. |

Verb-usage rotation + player-mention rotation were built but ROLLED BACK (see below).

### Negative results (things that DID NOT work)

Recorded here because "we tried it and it hurt" is a useful signal for future iterations.

- **v15 dense-frames experiment** (8 frames at 0.3 s intervals): hallucination went UP from 15 % → 33 %. Denser sampling gave the model a bigger canvas to invent motion.
- **Verb-usage rotation** (bans overused action verbs across a rolling window): hallucination went UP because "safe" verbs (`waits`, `stands`, `over`) got banned when the model legitimately needed them for repetitive play states. The model reached for `strides`, `sweeps`, `claims` instead — all specific event claims that judge could flag.
- **v9 stacked filters** (playerist + strict arbiter + agreement-required + frame verifier): hallucination went UP to 20 % from v8a's 16 %. The arbiter still had to pick SOMETHING; it settled into short-safe fallbacks that were themselves speculative. Coverage stayed at 100 % but humanlike dropped to 3.33 — the layered filters produce prose that reads as robotic.
- **v12 Gemini + playerist**: hallucination 31 %, essentially unchanged from Gemini baseline (32 %). Only gpt-5.5 responds meaningfully to playerist. Smaller models produce more player-named sentences but still speculate about events at the same rate.
- **Hybrid dual-vision producer** (gpt-5.4-mini + Gemini in parallel, arbiter merges): v8a variant hit 16 % hallu vs v5 single-model 17 %. Not worth the 3× code complexity and 2× latency. 4/11 hallucinations were correlated across both models (same wrong claim in both — merge can't catch).

### Ablations to isolate the round-2 hallucination regression

After adding all six user-feedback improvements, Pipeline 1's hallucination jumped from 15 % to 28 %. Two ablations to isolate which addition:

| Variant | Change vs baseline | Hallu | Human | Cover | Lines |
|---|---|---|---|---|---|
| v13_live (all improvements on) | baseline | 28 % | 3.94 | 100 % | 50 |
| v16 (replay detection off) | disabled REPLAY prompt block | 27 % | 3.83 | 97 % | 48 |
| **v17 (generic-first hoisted)** | added "GENERIC OVER INCORRECT" at prompt top with banned-verb list | **9 %** ✓ | 3.22 | **73 %** ✗ | 32 |

**Finding:** the "generic-first" prompt reordering — moving the anti-hallucination framing to the very top and adding a banned-verb list — cut hallucination by 2/3 (28 % → 9 %). But the ban-list dropped the model into over-conservative mode (32 lines vs 50, coverage 73 %).

**Removing replay detection barely moved anything** (28 % → 27 %). Replay wasn't the primary culprit — the model's tendency to over-narrate visible-but-ambiguous moments was.

The next iteration (v18) is **generic-first framing WITHOUT the strict ban list** — keep the top-of-prompt reminder but remove the verb blocklist. Landed at 17 % hallu, 3.34 humanlike, 79 % coverage — see round 3 below for what came next.

### Round 3 iterations — v18 through v20 (2026-07-02 → 03)

Building on Pipeline 1 (gpt-5.5 + playerist + rich context), stepped through:

| Variant | Change | Hallu | Human | Cover | p90 lag | Lines |
|---|---|---|---|---|---|---|
| v18 | soft generic-first + anchor frames (−5 s, −10 s) | 17 % | 3.34 | 79 % | 7.7 s | 35 |
| v19 | **two-stage: safe-draft vision + gpt-5.4-mini polisher** | 10 % | 2.90 | 88 % | 5.7 s | 40 |
| v20 (mini polisher) | same as v19 | 11 % | 2.90 | 88 % | 5.7 s | 40 |
| v20 (gpt-5.5 polisher) | polisher upgraded to gpt-5.5 | 11 % | 3.37 | 91 % | 5.7 s | 37 |
| v20 + LLM MOTION prefix | asked vision to emit `MOTION: high/normal/low/replay` | 90 % NO_CALL | — | — | — | 11 |
| v20 + prompt motion hint | CPU pixel-diff injected into vision prompt as text | 19 % | 3.43 | 97 % | 5.1 s | 42 |
| **v20 CPU-only replay** ★ current | CPU pixel-diff short-circuits to canned replay line; no prompt change | **15 %** | **3.54** | 91 % | 5.4 s | 39 |

Key architectural finding: **splitting content-detection from prose polishing into two LLM calls** (safe-draft vision + text polisher) got us near the offline-batch hallucination floor (8 %) at live speeds. The polisher's mandate is "reword, don't add claims" — its input is text-only, so it cannot invent new events.

Insights confirmed in this round:
- **In-prompt MOTION protocols confuse the vision LLM** — 90 % NO_CALL rate. Removed.
- **CPU-side pixel-diff for slow-mo detection works reliably** — ~5 ms/burst, thresholded at `motion_ratio < 0.2` matches the offline slow-mo detector on real replay segments.
- **When CPU says replay, skip the vision call entirely** — canned "here's the replay" line is more accurate than any LLM output on those frames.
- **gpt-5.5-pro was tested batch-only** — 30 s per burst vs gpt-5.5's 6 s, all outputs empty (reasoning tokens ate the budget). Not viable live.

CPU slow-mo detector: `experiments/ai_commentator/detect_slowmo.py` (offline analysis over 545 master frames) + inlined `compute_motion_hint()` in `live_srt_run.py` (5 ms per burst using numpy + PIL).

Best live Pareto point right now: **v20 with CPU replay short-circuit, gpt-5.5 polisher**. 15 % hallucination, 3.54 humanlike, 100 % subject-present, 91 % coverage, 5.4 s p90 lag.

### Frame-config sweep on a 60 s problem slice

To iterate faster on the hallucination-heavy sub-heavy stretch (master 180-240 s = Veratschnig booking + Becker/Sieb sub + Weiper on), the source was clipped to a 60 s slice (`slice_subs_60s.mp4`) and four frame configurations compared:

| Sweep | Frames × interval | Window | Real events caught | Notable hallucinations |
|---|---|---|---|---|
| baseline | 4 × 0.55 s | 2.2 s | booking, Becker sub | (none obvious) |
| wider | 4 × 1.0 s | 4.0 s | booking, sub | 🚩 hallucinated a Becker goal (`"beating Klaus from close range!"`, `"levelling it"`) |
| more_wide | 6 × 1.0 s | 6.0 s | booking, Weiper→Tietz sub | false replay call |
| **anchor** | 4 × 0.55 s + anchors from −5 s and −10 s | 2.2 s + history | booking (correct ref alias), Weiper→Tietz sub, Becker sub | still hallucinated a Becker goal (`"squeezes it in from a tight angle!"`) |

Judge scores on the shortened runs are unreliable because `judge.py` loads frames from the FULL 5-min slice directory by `video_time_s`, but shortened-clip runs have `video_time_s` reset to 0. The judge compares "Veratschnig receives the booking" (master-time 186 s) to the frame at 6 s (kickoff area) and flags every line. **Sweep judge scores are artefacts — trust the qualitative transcripts.**

Anchor frames from further back help sub identification (historical context grounds "who was on the bench" claims). They don't help distinguish "chance near goal" from "goal scored" — that needs mid-execution motion inside the current burst.

### Snapshots + stable share links

Every `build_public_page.py --snapshot` copies the current results.html + all referenced MP4s / JSONLs / judge JSONs into `/var/www/html/experiments/ai_commentator/YYYYMMDD-HHMM/`, plus an internal `notes.md` (unlinked from public pages) documenting model + prompt combos. Shared links stay stable across future iterations.

Key snapshots so far:

| Snapshot | State |
|---|---|
| 20260701-1158 | Pipeline 1 live, Pipeline 2 batch (v5) |
| 20260701-1254 | both live, initial Pipeline 1 + Pipeline 2 numbers user reviewed |
| 20260701-1300 | measured p90 lag as stat card |
| 20260702-0541 | fixed delay as stat card |
| 20260702-0559 | with judge-rules explainer collapsible |
| 20260702-1551 | with 6 round-2 improvements (referee/waiting/vocab/replay/crowd) — Pipeline 1 hallu regressed to 28 % |

### v6.html — live results page

Behind `https://sip.dev.gw.01.agora.io/experiments/ai_commentator/v6.html`:
- Full leaderboard with 14 metrics, colour-coded good/bad
- 4 side-by-side transcript columns with per-line judge verdicts, star ratings, hallucination flags, and short judge rationales
- Audio-track toggle over the video player: original broadcast + 8 AI variants (v5 EN + FR, v8a EN + FR, v8d EN + FR, v8 base, v7, gpt55_playerist, v4, and MP4 output for every variant listed above)

### Artefacts (added since first-pass docs)

| File | Contents |
|---|---|
| `experiments/ai_commentator/run_v5.py` | v5 runner: sub state, trigram dedup, frame carry-over, dynamic gate |
| `experiments/ai_commentator/run_gemini.py` | Gemini variant of the v5 pipeline |
| `experiments/ai_commentator/run_gpt55.py` | gpt-5.5 baseline runner |
| `experiments/ai_commentator/run_gpt55_variant.py` | gpt-5.5 prompt variants (`quiet`/`long`/`playerist`) |
| `experiments/ai_commentator/run_gpt54.py` | gpt-5.4 baseline runner |
| `experiments/ai_commentator/run_v7_hybrid.py` | v7 hybrid: dual vision + rule-based merge |
| `experiments/ai_commentator/run_v8_hybrid.py` | v8: dual vision + arbiter-judge with frame + rich context |
| `experiments/ai_commentator/run_v8_variants.py` | v8a/b/c/d variants (strict rubric, gpt-5.5 arbiter, both, conf-gated vision) |
| `experiments/ai_commentator/run_v9.py` | v9: playerist + strict arbiter + agreement-required + frame verifier |
| `experiments/ai_commentator/run_v10_single_playerist.py` | v10: single mini + playerist (control) |
| `experiments/ai_commentator/run_v11_gpt54_playerist.py` | v11: gpt-5.4 + playerist |
| `experiments/ai_commentator/run_v12_gemini_playerist.py` | v12: Gemini + playerist |
| `experiments/ai_commentator/rich_context.py` | Extended pre-game context — all 40 roster entries + narratives + manager/referee/storyline |
| `experiments/ai_commentator/score.py` | Deterministic 12-metric scorer; writes `leaderboard.json` |
| `experiments/ai_commentator/judge.py` | gpt-5.5-as-judge for hallucination / human-likeness / subject-present / coverage |
| `experiments/ai_commentator/tts_round.py` | Generic emotion-tag + TTS (EN+FR optional) for any variant |
| `experiments/ai_commentator/tts_v8.py` | v8-specific TTS (uses arbiter's pre-generated FR translation) |
| `experiments/ai_commentator/build_v6_results_page.py` | v6 page generator (leaderboard + audio toggle + transcripts + judge overlays) |
| `experiments/ai_commentator/leaderboard.json` | Source of truth for all variant scores |
| `experiments/ai_commentator/judge_*.json` | Per-variant raw judge verdicts (full-sample) |
| `match_data/m05_uni_md33/team_aliases.yaml` | Per-team alias bank (nickname / kit colour / manager / place) |
| `experiments/ai_commentator/live_srt_run.py` | Live SRT ingest + realtime vision loop + TTS EN+FR (both pipelines) |
| `experiments/ai_commentator/repetition_helpers.py` | Verb / player / referee rotation helpers + waiting-line detector |
| `experiments/ai_commentator/rich_context.py` | Full 40-player context + manager fingerprints + storylines + referee profile |
| `experiments/ai_commentator/mux_with_crowd.py` | ffmpeg amix step: crowd bed at −22 dB under AI commentary |
| `experiments/ai_commentator/build_public_page.py` | Snapshot-aware public results page (Pipeline 1 / Pipeline 2 labels) |
| `plan_tracking.md` | Player-tracking Tier A/B/C plan (not started; grounding via YOLOv8/OCR/pose) |
| `experiments/ai_commentator/detect_slowmo.py` | Offline slow-mo detector — numpy pixel diff across master frames, exports `motion_analysis.json` |
| `experiments/ai_commentator/compare_pro.py` | gpt-5.5 vs gpt-5.5-pro batch comparison (20-burst sample) |

### Migrating this experiment to a GPU box

The `experiments/` directory is `.gitignore`d — moving to a new server needs an out-of-band copy. What to bring for the tracking work (`plan_tracking.md`):

Required source files (~10 MB total, all in `experiments/ai_commentator/`):
- `run_*.py`, `live_srt_run.py`, `detect_slowmo.py`, `mux_with_crowd.py`
- `rich_context.py`, `repetition_helpers.py`, `score.py`, `judge.py`, `tts_*.py`
- `build_public_page.py`, `build_v6_results_page.py`
- All `commentary_*.jsonl` and `judge_*.json` (evaluation reference data)
- `leaderboard.json`, `gold_soniox_5min.jsonl`, `motion_analysis.json`
- `frames/` — 545 pre-extracted master frames the judge and detect_slowmo.py read (~40 MB)

Required match data (~370 MB):
- `match_data/m05_uni_md33/roster.json`, `sr_cache.json`, `team_aliases.yaml`, `match.json`, `keyterms.txt`
- `match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json`

Required source video (~110 MB):
- `/tmp/v2v_compare/slice_5min.mp4` OR `clips/m05_uni_eval_25min/source.mp4` — the 5-min slice used across all variants

Do NOT copy: `*.wav`, `*.mp4` in `experiments/ai_commentator/` (5+ GB, regeneratable from JSONL + source clip + TTS re-run).

Quick recipe:
```bash
tar czf ai_commentator_src.tgz --exclude='*.wav' --exclude='*.mp4' \
  experiments/ai_commentator/ match_data/m05_uni_md33/
scp ai_commentator_src.tgz newbox:~/
scp /tmp/v2v_compare/slice_5min.mp4 newbox:/tmp/v2v_compare/
```

On the new box, install: `openai`, `numpy`, `Pillow`, `ultralytics` (for YOLOv8 tracking work), `opencv-python` (for optical flow if needed). Set the same `.env` values (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`).

### Old text (kept for context on the initial v3→v4 progression)

### Quality bar — answered (kind of)

The 14-metric framework gives a working numeric definition of quality. No single metric dominates; "best" depends on what you weight. Soniox is the target for cadence/coverage/TTR, but for hallucination-rate and team-alias variety the AI variants can beat it (Soniox just says "Mainz" five times — no incentive not to).

### Artefacts (added in v5 / v6)

| File | Contents |
|---|---|
| `experiments/ai_commentator/run_v5.py` | v5 runner: sub state, trigram dedup, frame carry-over, dynamic gate |
| `experiments/ai_commentator/run_gemini.py` | Gemini variant of the v5 pipeline |
| `experiments/ai_commentator/run_gpt55.py` | gpt-5.5 baseline runner |
| `experiments/ai_commentator/run_gpt55_variant.py` | gpt-5.5 prompt variants (`quiet`/`long`/`playerist`) |
| `experiments/ai_commentator/score.py` | Deterministic 12-metric scorer; writes `leaderboard.json` |
| `experiments/ai_commentator/judge.py` | gpt-5.5-as-judge for hallucination / human-likeness / subject-present / coverage |
| `experiments/ai_commentator/tts_round.py` | Generic emotion-tag + TTS (EN+FR optional) for any variant |
| `experiments/ai_commentator/build_v6_results_page.py` | v6 page generator (leaderboard + audio toggle + transcripts) |
| `experiments/ai_commentator/leaderboard.json` | Source of truth for all variant scores |
| `experiments/ai_commentator/judge_*.json` | Per-variant raw judge verdicts |

### Five rules added in v4 (all generic, pre-game info only)

1. **Scoreline rule** — never state the scoreline unless score just changed this burst, or it's the final 5 min and tension warrants it. Eliminates v3's "still 1-1" filler.
2. **Team alias rotation** — `match_data/<match>/team_aliases.yaml` lists role tokens, nicknames, kit colours, manager possessives, place names. Runner counts uses in last 3 lines and tells the model on each call which short names to AVOID this turn and which aliases are fresh.
3. **Substitution-board recognition** — fourth-official LED reads as `RED top / GREEN bottom`. Prompt instructs the model to interpret as "X off for Y" and resolve numbers via the roster, not as a clock or score.
4. **Set-piece team attribution** — name a team for a throw / FK / corner only when clearly visible. Otherwise describe set piece without naming a team. Avoids confidently-wrong attributions.
5. **Filler reduction** — booth-busy gate widened, prompt explicitly says "speech is reserved for moments of consequence; routine possession is silent."

### Artefacts

| File | Contents |
|---|---|
| `experiments/ai_commentator/run_v3_balanced.py` | v3 — first version that landed natural-sync + per-player insights |
| `experiments/ai_commentator/run_v4.py` | v4 — adds the 5 generic rules |
| `experiments/ai_commentator/commentary_v4.jsonl` | All v4 vision attempts (accepted + rejected, with reason) |
| `experiments/ai_commentator/commentary_v4_scheduled.jsonl` | Only accepted v4 lines with timing |
| `experiments/ai_commentator/commentary_v4_en_tagged.jsonl` | EN + emotion tag (input to TTS) |
| `experiments/ai_commentator/commentary_v4_fr_tagged.jsonl` | FR translation + emotion tag |
| `experiments/ai_commentator/tts_v4_en.py`, `tts_v4_fr.py` | Emotion-tagging + eleven_v3 TTS |
| `experiments/ai_commentator/gold_soniox_5min.jsonl` | Soniox gold-corrected STT, 5-min slice |
| `experiments/ai_commentator/build_v4_results_page.py` | 3-column comparison page generator |
| `experiments/ai_commentator/team_aliases.yaml` | (lives under `match_data/<match>/`) per-team alias bank |

### Listening / reading links

- https://sip.dev.gw.01.agora.io/experiments/ai_commentator/ — index
- https://sip.dev.gw.01.agora.io/experiments/ai_commentator/v4.html — 3-column comparison page (Soniox | AI EN | AI FR, click any line to seek)
- https://sip.dev.gw.01.agora.io/experiments/ai_commentator/v4_brit_synced.mp4 — AI EN
- https://sip.dev.gw.01.agora.io/experiments/ai_commentator/v4_fr_synced.mp4 — AI FR

### Quality bar — when is this good enough?

Currently we judge by listening + transcript comparison against Soniox gold. Open question: is there a useful numeric metric? Candidate signals:
- Line count per 5 min (vs Soniox baseline of ~34)
- Distinct team-alias count (variety)
- Player-name accuracy on visible plays
- Fraction of lines that match a Soniox turn within ±3 s on subject (player named, action described) — would need a judge LLM

### Open follow-ups

1. **v5** — sub-event memory across calls (state-tracked); trigram dedup; carry one frame from previous burst for visual continuity; dynamic booth-busy gate (longer during routine play); stronger NO_CALL.
2. **Gemini parallel run** — same 4-frame pipeline via Google's vision model. A/B with gpt-5.4-mini to see if one catches things the other misses.
3. **Live wiring** — current is offline-batch on a stored MP4. Real test is on an SRT-in / Agora-out live pipeline — requires backend video-delay buffer of ~1.4 s to hide pipeline lag (vision+TTS ~1.7 s, natural reaction allowance 0.3 s).
4. **Player tracking signal** — even a cheap "what shirt numbers are visible right now" emitted from a separate cheap inference + fed into the prompt could remove a large fraction of misIDs.
5. **Hybrid with STT** — when a broadcast STT IS available, use it as ground truth for what's happening and have the AI commentator generate the alternate-language version. Different shape of problem, possibly easier.

---

### Round 4 — structured events-detector eval + tracker + fact-phrase blend (2026-07-06 → 10)

A pivot from "one model writes commentary" to **grounded facts + real language, chosen by a final LLM**. Everything runs on the GPU box; pages are at **`https://sa-dev.agora.io/experiments/…`** (SSL; added as an additive `/experiments/` static location on the sa-dev server — does not touch the `:8095` app proxy).

**(a) Events-detector eval — OpenAI vs Gemini vs tracker.** Shared prompt `prompts/events_detector_v1.txt` (from the other server) emits strict JSON: phase / possession(team,third,side,shirt#) / ball_state / events[]. Runner `run_vision_eval.py` (OpenAI `responses` + Gemini `generateContent`, `--frames` sweep). Comparison + human-scoring page `build_vision_eval_page.py` → `/experiments/vision_tracker_eval/<test>/` (time-aligned single-scroll grid, tick-per-cell, per-reviewer submit via `submit_server.py` behind nginx `/vte_submit`). Findings on the 5-min slice:
- gpt-5.5: conservative, higher-precision events; **latency flat across 2/4/6/8 frames** (~7-8s, reasoning-bound not image-bound; consistent with prod vision p90).
- gemini-3-flash: ~2x events + shirt#s, near-everything "high-confidence" (mis-calibrated), occasional team flips; latency rises at 8 frames.
- No column is "best" outright — LLMs for events/possession, tracker for objective location. Human ticking is the arbiter (UI built; not yet scored).

**(b) Tracker (round 2).** `run_tracker_detector.py` on the T4: roboflow/sports player+ball YOLO weights (`tracker_models/`, gdown) + kit-colour team + **pitch homography** (inlined `SoccerPitchConfiguration.vertices` + `cv2.findHomography`) → **objective ball third** on ~99/271 bursts (orientation from keeper positions). Objective third distribution (def 43 / mid 35 / att 21) **contradicts the LLMs' "attacking" bias** — so `third` is dropped from LLM columns and shown only for the tracker. **easyOCR reads 0/271 jersey numbers** — genuine 720p resolution limit (players ~70px, numbers ~12px), not a bug; needs 1080p+/OCR or a data feed.

**(c) Gemini STT re-test** — see the V2V section update above (translate model: 104% coverage, 22% WER, real-time; reverses the old negative).

**(d) Fact+phrase blend composer.** `run_blend_live.py` — one commentary track built **live over SRT**: short standalone Soniox phrases verbatim (harvested by `harvest_soniox.py`: ≤4s + sentence-complete + conf≥0.8 + LLM football-relevance filter → ~26 phrases) as anchors; a roster-aware **chooser LLM** fills gaps from a grounded menu {vision fact, tracker truth, broadcaster-named players}. Guards: event/subject/lull dedup + near-duplicate output rejection; Soniox preempts the gate. **Real player identity wired in** — authoritative Sportradar lineup (`sr_cache.json` → number/name/position/team/starter) + broadcaster STT mentions; the chooser only names a *validated* lineup player whose team matches, else team/role (kills invented/cross-team names). EN + FR (translate + ElevenLabs), muxed to synced videos. Results page `build_blend_page.py` → **`/experiments/ai_commentator/blend/`** (Original / AI-English / AI-French audio toggle + 3 transcript columns).
- **Honest status: strong demo, not customer-ready.** Blockers: (1) per-moment player identity still fallible (lineup validates *directory*, not who-has-the-ball — vision can still map to the wrong *real* player, e.g. "Kohn takes the goal kick"; fix = 1080p OCR / tracking feed); (2) accuracy unverified by humans (tick UI ready); (3) generation is functional not emotive; (4) the real-phrase pillar only exists for *already-commentated* footage. See `SESSION_STATE.md` for full file map.

## 3. Server-side BWE / ABR feasibility (C++ Agora SDK)

### Status

**Side experiment, parked.** Confirmed that server-side Agora SDK BWE callbacks measure the server box's own legs to the Agora edge, NOT the audience downlink. This means server-side BWE cannot drive per-viewer ABR. The viewer's link quality must be measured client-side (e.g. via the Web SDK `network-quality` event) and reported back.

### Sources

- **Bundle (evidence):** `experiments/bwe_abr/bwe-abr-bundle.tar.gz` (symlink to `/home/ubuntu/bwe-abr-bundle.tar.gz`). Self-contained C++ sample + shell scripts + browser test client; full reproduction of the negative result. The bundle's `README.md` is the canonical writeup.
- **Plan (build spec):** `experiments/bwe_abr/plan_bwe.md`. Detailed plan for a standalone C++ adaptive-bitrate publisher with 6-level ladder and hysteresis controller. **Plan does not match the bundle's finding** — it was written before the feasibility experiment. If reviving the plan, factor in the bundle's evidence that the server-side BWE signal doesn't carry per-viewer information.

### Key findings (from the bundle)

| Callback | What it measures (per the bundle's experiment) |
|---|---|
| `onUplinkNetworkInfoUpdated.video_encoder_target_bitrate_bps` | Server uplink (box→edge) — the content leg, useful for the server's own congestion control |
| `onDownlinkNetworkInfoUpdated.bandwidth_estimation_bps` | Server downlink (edge→box) — only meaningful when a remote peer also publishes; never the audience's downlink |

Throttling `tc qdisc` on the box's NIC moves the uplink callback but **doesn't move `bwe`** unless the offered traffic exceeds the cap. Throttling the receiver's NIC doesn't move `bwe` at all.

### Re-run instructions

```bash
cd /tmp
tar xzf /home/ubuntu/commentary/experiments/bwe_abr/bwe-abr-bundle.tar.gz
cd bwe-abr-bundle
cat README.md   # full setup, build, and run instructions
```

The bundle expects an Agora SDK at `~/sdk-testing/Agora_Native_SDK_for_Linux_FULL/...`. The commentary repo's Agora SDK at `go-audio-video-publisher/agora-sdk/agora_sdk/` is the same v4.4.32 binary and headers (language-agnostic), so the bundle's source can build against either.

### Open follow-ups

If product needs per-viewer ABR, the **v2 design** is client-driven: browser measures its own downlink and reports back over the existing WebSocket / control channel. The server then drives variant selection from the reported metric. The C++ ABR controller in `plan_bwe.md` is still useful for the *server-side execution* (switching pre-encoded variants on keyframe boundaries) — but the *trigger* must come from the client, not from `onDownlinkNetworkInfoUpdated`.

---

## Round 5 — 1080p vs 720p player identity (tracker + gpt-5.6 vision)

**Status:** done. **Clip:** Dortmund vs Eintracht, 5-min extract 20:00–25:00 (native
1920×1080 vs downscaled 1280×720). **Question:** does 1080p give better player identity,
and should OCR read the shorts number too? Full detail →
[L2/resolution_tracker_eval.md](../L2/resolution_tracker_eval.md).

Upgraded the "tracker" from stateless per-frame detection to **BoT-SORT multi-object
tracking + OCR-vote-and-propagate** (`run_tracker_tracked.py`): each player gets a
persistent id, OCR runs opportunistically, the number is voted over the track and
propagated to every frame. `read_number` now OCRs the **shirt and the shorts**.

| Metric | 720p @5fps | 1080p @5fps | 1080p @10fps |
|---|---|---|---|
| easyOCR reads (shirt / shorts) | 178 / 17 | 220 / 30 | 329 / 54 |
| Tracks named | 38 | 47 | 78 |
| Frame identity coverage | 7.9% | 9.6% | **19.1%** |
| gpt-5.6 vision numbers read (of 68) | 28 | 30 | — |

Headline: **1080p helps easyOCR/tracker ~+22–28%** (shorts benefit most, +76%) but
**barely helps the vision LLM** (+7%, resolution-robust). **Frame rate matters more than
resolution** — 5→10 fps *doubled* identity coverage. The real bottleneck is **tracking
fragmentation** (BoT-SORT ReID off + broadcast cuts → ~600 tracks for ~22 players), not
pixels. Next lever: **ReID + shot-boundary handling** (the SoccerNet Game-State-
Reconstruction stack), and a **hybrid** where the vision LLM reads the carrier number and
the tracker propagates it.

---

## Round 6 — the recommended live pipeline + public 5-column page

**Status:** done, live at `https://sa-dev.agora.io/experiments/ai_commentator/blend/`.
The best commentary we can currently produce for Mainz vs Union, built **live over the
SRT feed** and voiced EN + FR.

**Recommended pipeline (multi-rate hybrid, 720p).** See
[L2/resolution_tracker_eval.md](../L2/resolution_tracker_eval.md) for why 720p:
1. **STT anchor** (Soniox short + high-conf phrases) — preempts everything; real
   broadcaster words, zero hallucination.
2. **Vision spine** (gpt-5.6, 4-frame bursts, **720p**, parallel calls) — events +
   possession + carrier number. Low confidence is dropped; a player is **named whenever the
   shirt number is readable + roster-valid** (either high OR medium possession — a filled
   number already passed the detector's "unambiguously readable" bar, ~44/271 bursts here);
   otherwise the line stays team/role level.
3. **Tracker** (BoT-SORT + OCR-vote-and-propagate) — async identity/location truth,
   off the critical path.
4. **Blend chooser** (gpt-5.4-mini) — STT-first; else generates authentic play-by-play
   from vision/tracker. Names the **pass receiver** ("Trimmel collects it"), names a player
   whenever the menu hands one (vision number or a broadcaster mention nearby, never a guess),
   enforces **verb variety** via an avoid-list (same topic fine, repeated verbs not), knows
   the **match clock** (2nd half, ~77', 1-1) and references it *sparingly/varied*, and fills
   rare dead air (>40 s) with a scene note. A **fresh named ball-carrier in open play can
   preempt** the trailing pause of the previous line (once its audio has finished) so genuine
   passes are called promptly — gated on `phase=open_play`, so replays/stoppages never trigger
   a name. The **tracker contributes territory + shape** (ball-third via homography, "numbers
   back") on ~half the generated lines, and is the sole grounding on a few.
5. **TTS** (ElevenLabs flash) EN + FR.

**Public page.** Five columns on one timeline: **STT / Vision / Tracker** (the three live
input signals, labelled by *function* only) + **Blend EN / FR**. The columns are named by
what they do, but the specific **products/models are never disclosed** (no Soniox / OpenAI /
gpt / BoT-SORT strings leak to the page). Built by `build_hybrid_page.py` from
`soniox_live_short.jsonl` (STT), `oai_col.jsonl` (Vision, `render_oai_col.py`),
`tracker_col.jsonl` (Tracker, `render_tracker_col.py`), and `commentary_blend_live.jsonl`.
Shared-timeline grid, scrub-sync, click-to-seek, audio toggle Original / AI-EN / AI-FR.

### End-to-end latency applied to live (720p)

A live deployment delays the outgoing video by a buffer and speaks into it. Budget by
line type:

| Stage | STT-anchored line | Vision-generated line |
|---|---|---|
| SRT + ffmpeg transport | ~0.3 s | ~0.3 s |
| Soniox partial→final | ~1–2 s | — |
| Frame-burst cadence | — | ≤0.55 s |
| Vision inference (gpt-5.6, 720p) | — | **~4.5–6 s median, ~10 s p90** (measured) |
| Chooser LLM (gpt-5.4-mini) | ~0.6 s | ~0.6 s |
| TTS (ElevenLabs flash) | ~0.4 s | ~0.4 s |
| Placement lag (`NATURAL_LAG_S`) | 0.3 s | 0.3 s |
| **End-to-end behind live** | **~2.5–3.5 s** | **~6.5–8 s median, ~12 s p90** |

### MEASURED end-to-end (true-live run, `run_blend_true_live.py`)

The latency budget is now **proven in-loop**, not estimated: a run with vision inference
(gpt-5.6, 4-frame 720p bursts, 3 parallel workers) called live inside the SRT loop, STT
availability-gated to realistic arrival, per-line latency logged, and audio rendered as
"live with a 10 s buffer" (a line about moment *t* placed at `t + max(0, behind_live − 10)`,
so late lines audibly slip). Results (`latency_report.json`, 105 vision calls, 50 lines):

| Measured | median | p90 | max |
|---|---|---|---|
| Vision call (gpt-5.6, 720p, shared 4-core box) | 7.7 s | 13.2 s | 20.2 s |
| Behind-live — vision lines | **10.7 s** | **16.3 s** | 19.0 s |
| Behind-live — STT lines | ~3–6 s | — | — |

**16 of 50 lines exceeded the 10 s buffer** (all vision lines; max slip ~7 s). Honest
conclusions:
1. **STT-anchored commentary is comfortably live** (any buffer ≥6 s).
2. **Vision commentary on THIS box needs a ~16 s buffer** to land on the play at p90 —
   the earlier ~7 s estimate is disproven. The gap vs the isolated ~4.5 s probe is
   contention: the same 4 cores run 2× ffmpeg, TTS, translation, and 3 vision encoders.
3. **The fix is engineering, not modelling:** a dedicated inference host (no ffmpeg/TTS
   contention), smaller frames for the vision call, fewer output tokens, or a faster
   model tier — each attacks the 7.7 s median directly. Until then, quote **~10–15 s
   behind live** for vision-grounded lines, not 7 s.

### FINAL production recipe (drop-late policy — sync guaranteed)

Follow-up bench + run fixed the latency: an idle-box sweep (`latency_bench.py`) showed the
tier was never the problem — **gpt-5.6 fed 960×540 frames is ~40% faster than at 1280×720**
(3.9 s vs 6.3 s median; gpt-5.4-mini is 2.4 s but its possession-team agreement with the
reference detector was only 10/16 — rejected for quality). Final config in
`run_blend_true_live.py`:

- **Fixed broadcast delay: 10 s** — the number to quote.
- **Vision: gpt-5.6, 4-frame bursts at 960×540, 3 parallel workers** — measured live
  (150 calls, with full pipeline contention): median 5.7 s, p90 8.3 s, max 11.4 s.
- **Drop-late policy:** stale detections are skipped before speaking (`STALE_S = buffer − 3`);
  any line whose true latency still exceeds the buffer is cut from the audio, never shifted.
  **Every line heard is exactly on its play.**
- **Result (final 5-min live run): 42/45 lines survived (93%)** — 18 STT + 24 vision, all
  ≤ 9.6 s behind live; 3 dropped (a quiet beat, not an error). Dense phrase sequences may
  start up to ~4 s after their moment when the previous line is still being spoken
  (no-clobber; normal booth behaviour).

So the customer-facing statement is: **"AI commentary runs 10 seconds behind the live feed,
with commentary guaranteed in sync with the pictures; ~93% of candidate lines make the
window on current shared hardware."** A dedicated inference host raises survival further
and could support an 8 s delay.

### Safe vs eager — two final-LLM stages, A/B on the page

The published page carries TWO complete blends over the same live run, toggleable as
audio tracks and shown as side-by-side columns:

- **Safe** — the production recipe above. gpt-5.4-mini chooser, one detection per line.
  **45 lines, 95.7% survival**, max gap 24.8 s.
- **Eager** — a *separate final-LLM architecture* (`eager_commentator` in
  `run_blend_true_live.py`, `BLEND_MODE=eager`): a gpt-5.5 **commentator** given a
  6-second WINDOW of detections (motion, not a snapshot), its own timestamped commentary
  history (told to build narrative threads), and a broadcast-craft brief — grounding rules
  identical to safe. Two mechanisms make its coverage match safe's:
  1. **Hedge**: every moment fires BOTH final stages in parallel; if the gpt-5.5 line
     can't be ready inside the remaining sync budget, the fast safe line is used instead
     of dropping. Coverage floor = safe; craft ceiling = eager. Each line records its
     `stage` (`eager` vs `safe_fallback`).
  2. **Placement-aware pacing**: the earlier eager coverage loss (82.6%) was mostly
     *self-collision*, not model latency — 7 of 9 drops were lines whose true timestamp
     was still covered by the previous line's audio. Moments already occupied by placed
     audio are now never attempted (applies to STT slots too).
  **Final eager run: 39 lines, 97.5% survival, 281 words, zero desync shifts** —
  13 eager-crafted + 10 safe-fallback + opener; slightly exceeds safe's survival while
  keeping crafted lines ("Kohn and Kemlein recycle it again from deep") wherever the
  clock allowed. Word limits are NOT the lever for this: eager lines are already 4-16
  words — its latency is model inference time, flat with output length.

Shared fixes in both: **pre-warm** of TTS/translate/vision connections plus a
**scoreboard-grounded scripted opener** put the first line at 0:01 (previously silent to
0:12 — cold-start latency cost the first STT anchor); a **desync guard** drops any line
whose no-clobber audio shift would exceed 1.5 s (one anomalous oversized write had
silently cascaded 64 s shifts — sync is now enforced at both gates).

**Judge control — the key calibration finding:** the whole-run style judge scores every
AI config 2/5 realism/variety, but the **real Bundesliga booth's own transcript scores
only 3/5 on the same rubric, with identical word-diversity (0.5)**. The text-only rubric
ceilings at ~3 for any live transcript, so the AI is one point off the human booth, not
three — and the automated judge is **saturated**: it cannot distinguish improvements
human listeners can hear. Next instrument: human A/B review (safe vs eager) on the page.
If reviewers prefer the eager voice, its coverage loss is recoverable (the windowed
narrative prompt on the fast model, or a dedicated inference host) — engagement beyond
the ~3/5 booth ceiling is the known identity work (ReID tracker), not more final-stage
tuning.

---

## Round 7 — human-in-the-loop tuning process (rules + regression gates)

**Status:** live process, first review cycle distilled. Full deep-dive →
[L2/hitl_tuning_workflow.md](../L2/hitl_tuning_workflow.md) (also served at
`https://sa-dev.agora.io/experiments/ai_commentator/docs/hitl_tuning_workflow.md`).

The standing improvement loop: reviewers mark timestamped issues on the page →
each issue is distilled into a **GENERIC rule** (never a clip-specific patch) in the
ledger `experiments/ai_commentator/tuning_rules.yaml` → implemented one at a time as a
prompt clause or code gate → a rerun must pass the **regression gate**
(`eval_snapshot.py compare`: hallucinations ≤ baseline, survival ≥ 0.95, zero desync
shifts, first line ≤ 2 s) before acceptance; rules that would regress guarded metrics
are **rejected and kept in the ledger with the measured reason**. New baselines commit
with their rule.

**Cycle 1 COMPLETE (2026-07-20): ACCEPTED at trio-9 worst-of-3** — survival
[1.0, 0.974, 0.956], hallucinations [0,0,0], all 9 automated fixtures green, named
lines [11,11,10] vs baseline 4. Nine gate iterations were needed; every rejection
became a permanent deterministic guard (see
[L2/review_cycle_1_dispositions.md](../L2/review_cycle_1_dispositions.md) — the
per-comment disposition of all 21 reviewer comments: 16 addressed, 3 partial with
stated vision-bound residuals, 2 positives retained, 0 unaddressed). Highlights the
process caught that single-run gating would have shipped: a **false goal call**
("Mainz have scored!", 2 same-moment detections — now R10 corroboration), a
double-card "booked again" implying a sending-off, an ASR error propagating into
two languages, and a completion-order audio race. Review page for cycle 2:
`https://sa-dev.agora.io/experiments/ai_commentator/blend_v2/`.

**First cycle (reviewer: Alex, 21 comments)** → 9 generic rules: event-priority
(missed yellow card), content-floor (no pointless filler), fact-dedup (25 s window),
continuity (mark state reversals), pronoun clarity, precision-restraint (no invented
action manner), **French localization with versioned glossary** (6 comments — the
largest cluster), STT-sanity (ASR errors vetted against vision events, e.g.
"changes of foot" during a detected substitution), and the accepted product verdict:
**eager is the default voice** (4 explicit preferences vs 0 for safe — R9, accepted
without a gate run as a direct human A/B result).

---

## Round 8 — on-page feedback, Portuguese, and delay profiles (v4)

**Status:** live. Review pages carry a browser-native feedback system and three languages.
Full mechanics → [L2/hitl_tuning_workflow.md](../L2/hitl_tuning_workflow.md).

- **On-page feedback** (`submit_server.py` + injected UI in `build_hybrid_page.py`):
  click any cell to comment (tags + free text + 👍), a **Submit** button (any reviewer,
  many times) and a **PIN-guarded trigger** (one operator closes the round → writes the
  work order). Server-side round state machine (`feedback/rounds.json`); late comments to a
  closed round are rejected-but-archived. Security-hardened after a Codex review
  (path-safe ids, body cap, constant-time PIN, atomic writes, `version==current`).
- **Brazilian Portuguese** added as a third live track (`translate_pt` localizer + glossary
  + production voice) alongside EN/FR — Tiago's review lane.
- **Delay profiles** via `BLEND_DELAY_S`: **10 s** (gpt-5.6 structured vision) and a **6 s
  fast profile** (mini vision, tracker-corroborated team calls, high-conf naming). Published
  side by side for comparison. Open item: the gate records but does not yet *reject* on a
  profile mismatch — a profile change needs a ledger amendment for now.

Review URLs (v4, round now closed): `blend_v4_10s/`, `blend_v4_6s/`, `blend_v4_6s_vt/`
(the last is the reviewer-requested vision/tracker-only variant, `USE_STT=0`).

---

## Round 9 — reviews → generic rules + evals (v5), 2026-07-23

**Status:** complete. Full current-state write-up →
[L2/blend_pipeline.md](../L2/blend_pipeline.md).

Alex reviewed all three v4 profiles on-page (141 comments; captured with
`clip/profile/column` so 6s-vs-10s feedback is never conflated). Distilled into
cycle-3 rules — all generic, grounded only in pre-match data:

- **R12 entity-grounding**: a card/goal/sub naming a player must credit that player's
  roster team ("Kohn yellow for Mainz" → Kohn is Union). Prompt rule + deterministic
  fixture that **failed on the pre-fix snapshot** (pin-the-defect) and flipped green.
- **R13 camera-ban**: no "in the frame / dans le cadre" picture-description lines.
- **R7 extension**: `translate_fr` upgraded from bare translator to football-French
  localizer with a reviewer-maintained glossary (mirroring PT).

v5 verdict from Alex (159 comments): **71% 👍 across all three profiles** (identical
score for 10s, 6s, and 6s_vt — the STT-free variant holds up), camera lines zero,
FR calques gone. One leak: attribution errors on non-card lines ("Kohn steps forward
for the home side") — the guard was scoped too narrowly.

## Round 10 — deterministic attribution guard (v6), 2026-07-24

**Status:** live, round v6 open (`blend_v6_10s/`, `blend_v6_6s/`, `blend_v6_6s_vt/`).

The v5 leak proved prompt rules alone are unreliable on the 6 s mini model, so
attribution became a **code guard**: `enforce_attribution()` corrects (never strips)
any "player for \<wrong team\>" reference using the roster, register-matched, with
award-beneficiary and both-teams-named exclusions. Optimized **offline against Alex's
actual reviewed lines** before any live run: resolves 2/3 flagged attribution errors,
alters **0/50** 👍-good lines, touches 2/81 lines total. All fixtures green live on all
three profiles.

**Honest residual:** ~14 of Alex's ~43 distinct actionable issues are
perception-layer (wrong player identity, territory direction, team-only event lines)
— not rule-fixable; ~21 are French-phrasing refinements (R7 glossary growth, the next
addressable bucket). Ship-vs-park hinges on a **reliability-mode measurement**
(high-conf naming only + tracker-only spatial claims → hard error count), not yet run.

**Parked side-quests:** Gemini 3.5 Transcribe vs Soniox harness built
(`test_gemini_transcribe.py`) — the EAP preview models 404 on our key (wrong GCP
project; needs a key from the allowlisted project). Claude Opus 4.8 vision+chooser A/B
prepped — blocked on `ANTHROPIC_API_KEY`.

---

## Tennis commentator — Glinka vs Mayo (v3), 2026-07-26

**Status:** v1 and v2 are closed and preserved for comparison. V2 received no
submitted items before the user explicitly directed a v3 release; its zero-item
disposition passed. V3 is live and both profiles pass the worst-of-three,
placement, media, and rendered-speech gates. Full architecture →
[L2/tennis_pipeline.md](../L2/tennis_pipeline.md).

This is a sport-specific, isolated five-minute experiment over the Cary
Challenger source at `02:00:15–02:05:15`. It does not import football runner
state, use football SRT/frame paths, or share feedback storage. Its build gate
refuses media/model work whenever an actual football commentary worker is
active.

Key differences from football:

- The third review signal is a **logical score tracker**, not a spatial player
  tracker. It accepts only legal tennis transitions with confidence >= 0.86 and
  two consecutive reads.
- Glinka starts in blue at the far end, but the mapping flips after the first
  game; player identity is not permanently tied to court end.
- The compact graphic can omit sets and blank points. Missing fields carry
  forward only as candidates; legality/corroboration still gate commitment.
- Deepgram and Whisper STT are compared. Whisper's first output failed sanity
  checks (pathological repeated “Oh.” segments plus a prompt echo), so it is
  retained as rejected evidence and cannot feed commentary.
- English uses the tennis-specific ElevenLabs voice
  `kfU9VUUMjY4PWNoUfZ45`; FR/pt-BR retain the football voice IDs. Audio buffers
  and media remain tennis-only.

The page uses the same six columns as football and a separate append-only
review round on port 8092. Every feedback item receives a stable ID; the next
version cannot publish until `check_feedback.py` verifies an exact disposition
for every ID.

Final v1 result:

| Profile | Attempts passing | Selected survival | Maximum boundary-aware gap | Review page |
|---|---:|---:|---:|---|
| 10 s | 3/3 | 12/12 (100%) | 58 s | `v1_10s/` |
| 6 s | 3/3 | 12/12 (100%) | 58 s | `v1_6s/` |

The 6-second pre-publication run exposed a scheduler defect: it charged natural
spoken duration against the inference deadline, despite complete TTS being
ready in under the deadline. The corrected contract gates detector-to-complete-
TTS readiness and handles spoken duration with no-overlap placement. A fixture
pins that distinction. Both profiles were then regenerated, re-judged, media-
probed, and published atomically.

The v1 review found that 12 lines were too sparse and did not identify the
server often enough. v2 replaces the sparse schedule with deterministic
server-led score calls plus strictly literal rally, changeover, and service-game
context. Its immutable clip fixture produces 18 lines, 9 server references,
4 rally calls, and a 32-second maximum gap. Publication now hard-fails below
16 lines, below the category coverage minima, or above a 40-second gap.

Final v2 result:

| Profile | Attempts passing | Selected survival | Server / rally calls | Maximum gap | Review page |
|---|---:|---:|---:|---:|---|
| 10 s | 3/3 | 18/18 (100%) | 9 / 4 | 32 s | `v2_10s/` |
| 6 s | 3/3 | 18/18 (100%) | 9 / 4 | 32 s | `v2_6s/` |

V3 keeps 18 calls but replaces bare score updates with a structured
score-outcome layer. It derives the point winner, point run, game-point count,
saved game point, hold/break, and next server from the previous/current
corroborated scores. Literal rallies add only accepted score/server context.
Background is blocked at single game point, break point, deuce, advantage, set
point, match point, or unsupported tiebreak state.

Final v3 result:

| Profile | Attempts | Survival | Outcomes / pressure | Server / rally | Median / P90 | Gap / shift |
|---|---:|---:|---:|---:|---:|---:|
| 10 s | 3/3 | 18/18 | 8 / 4 | 10 / 4 | 11 / 15 | 32 s / 0 s |
| 6 s | 3/3 | 18/18 | 8 / 4 | 10 / 4 | 11 / 15 | 32 s / 0 s |

An independent Deepgram pass over each final AI-only WAV verifies the rendered
speech rather than only the source text. All six profile/language tracks pass.
This check caught and drove fixes for a pathological 39–63-second French TTS
response, an ambiguous French spoken score, Portuguese pluralization, and an
overlong rally localization before release.

Release-title convention: `AI Tennis commentator — vX ready for review`.

The television benchmark now covers five official WTA full-match sources in
three fixed five-minute windows each: four real commentary references across
grass, clay, and hard court (60 minutes), plus a 15-minute clean-world-feed
doubles control. Two references use official captions; two and the control use
transient audio-only STT. No source transcript or media is retained. The
timestamped surrogate transcript stores only timing, word count, consensus
category/function, and guarded short paraphrases.

The 202 consensus commentator turns average 3.37/minute, slightly below v2's
3.6 calls/minute. The difference is depth: TV median turn length is 17 words
(P90 53) versus v2's median 8 (P90 10), with 43% speech occupancy versus about
16% AI voice. Match narrative/stakes, tactics/patterns, and point
reaction/outcome account for 119/202 turns; bare score/server is only 23/202.

V3 implements the evidence-backed direction without adding more calls:
selected 10–18-word lines combine a legal score transition with its known
point outcome and score significance, plus literal rally evidence joined to
accepted server/score context. Tactical, technical, emotional, winner/error,
and shot-quality claims remain out of scope without new grounding. Full method,
per-source results, and the rerun command are in the L2 deep dive and
`experiments/tennis_commentator/benchmarks/tv_commentary_playbook.md`.

The implemented v3 contract is in
`experiments/tennis_commentator/plan_v3.md`. It routes commentary by accepted
point, game, set, and changeover state rather than wall-clock time alone:
opening points orient the viewer, developing games build the score story,
pressure points prioritize consequences, and changeovers permit concise
context.

---

## Round 11 — Gemini 3.5 Transcribe (EAP) vs Soniox v5, live ASR A/B (2026-07-29)

**Status:** measured; human adjudication pending (page below). EAP feedback due to Google.

Same 5-min clip, both engines real-time streamed with the full roster as biasing context
(Gemini `adaptation_phrases`, Soniox `context.terms`). "Gold" caveat: the reference IS
Soniox-derived (lightly corrected) — so results are framed as agreement + human
adjudication, not absolute truth.

| | Soniox v5 | Gemini 3.5 Transcribe **Live** (adapted) | Gemini 3.5 Transcribe **unary** |
|---|---|---|---|
| Words (gold 537) | **540** | 348 (~65% coverage, holes of 14–28 s) | **517 (96%)** |
| WER vs gold | **6.5%** | 42.8% (deletion-dominated) | 14.9% |
| Finalize latency | p50 **1.36 s** / p90 3.27 s (exact per-token) | ~2.0 / ~3.2 s (approx) | n/a (5 min in **3.2 s** batch) |

**Key findings:**
- The unary sibling transcribes 96% of the SAME audio → the live model's holes are a
  **streaming/VAD finalization defect**, not audibility. (Echo of the v2v
  `gemini-3.1-flash-live-preview` failure, improved 24%→65% but still disqualifying.)
- `adaptation_phrases` genuinely works: Maintz/Mapei/Kuhn/Turnbull → 0, roster hits 6→22,
  WER 50.1%→42.8%. No false forcing observed.
- Neither engine meets Google's own <1 s P90 finalize target on this material.
- Live API returns no word timestamps (unary only) — measurement + placement pain.
- **Soniox v5 remains the production choice.**

**Artefacts:** `test_gemini_asr_live.py`, `asr_adjudication_page.py`,
scratchpad `gemini_asr/` (runs + unary), adjudication page
`/experiments/ai_commentator/asr_adjudication.html` (word-aligned disputes with verdict
buttons -> feedback server version `asrgemini`). Unary needs **v1alpha** REST
(`audioTranscription` parts are hidden in v1beta). 25-min source media is absent on this
box — second-window test blocked until restored.

---

## Adding a new experiment

When you run a new experiment:

1. Add a subfolder under `experiments/` and store scripts + small text/JSONL artefacts there.
2. Don't store large binaries — preserve only what's needed to regenerate (and the scripts that do).
3. Add a new section to this page following the pattern above: status, source clip, headline result, artefacts, re-run instructions, open follow-ups.
4. Keep the headline result quotable in one table — that's what people will skim.

# Tennis commentator

An isolated five-minute AI commentary experiment for Daniil Glinka vs Aidan
Mayo at the 2026 Cary Tennis Classic. It mirrors the football review contract
without sharing its processes, ports, temporary paths, artifacts, feedback
state, or deployment directory.

Versions 1–3 are closed and preserved for comparison. Version 4 is the current
review round. It accepts all 48 v3 review items, removes every unverified
live-rally sentence, and compares 5-second and 2-second fixed-delay profiles.
Both profiles pass all three judges and every fail-closed gate with 14/14
selected lines, zero live-ball calls, and zero audio placement shift.

## Clip and verified context

- Source: `tennis_challenger_atp_challenger_cary_usa_men_singles_8960445_512k.mp4`
- Exact clip: `02:00:15` through `02:05:15`
- Initial scoreboard: 0 sets, 0 games, 0 points; Mayo serving
- Initial court identity: Glinka is in blue at the far end; Mayo is at the near
  end. They change ends after the opening game.
- Pre-match information is cutoff at the start of June 29, 2026. Match results
  and later information are deliberately excluded.

The context bundle in `config.json` comes from the official ATP draw, USTA
acceptance list, ITF profiles, and official ATP/Tennis Canada title reports.
V4 keeps the reviewed score-outcome layer and converts accepted score transitions into
point outcomes, runs, pressure, saved game points, holds/breaks, and the next
server. V3 review showed that single-burst rally claims could become stale or
mistake a first-serve fault for a live ball, so v4 forbids them in every
language. Unsupported tactics, strokes, winner/error, emotion, and momentum
remain silent.

The reproducible WTA television benchmark is documented in
`docs/ai/L2/tennis_pipeline.md` and
`benchmarks/tv_commentary_playbook.md`. It now covers four commentary
broadcasts across grass, clay, and hard courts (60 sampled minutes) plus one
15-minute clean-world-feed control. The timestamped surrogate transcript keeps
only category, function, timing, word count, and guarded paraphrases; it never
retains source transcript text or media. Future review-page and Slack release
titles use the exact format `AI Tennis commentator — vX ready for review`.

Rebuild or extend the reference corpus independently of a commentary version:

```bash
/home/ubuntu/commentary/.venv/bin/python build_tv_corpus.py
```

The same football-idle guard runs before every transient media, STT, or model
operation.

English tennis commentary uses ElevenLabs voice
`kfU9VUUMjY4PWNoUfZ45`. French and pt-BR retain the existing configured
football voices. Voice changes do not alter commentary text or review URLs,
but both delay profiles must be regenerated and pass the normal gates and
rendered-speech audit. Each profile's `render_manifest.json` pins the voice IDs
and SHA-256 of every rendered WAV and review MP4.

## Isolation and execution

`build_v4.sh` refuses to start media or model workloads while the football live
pipeline is running. Tennis uses:

- `artifacts/v1/` for immutable shared clip, STT, vision, and detector inputs
- `artifacts/v4/5s/` and `artifacts/v4/2s/` for separately generated,
  rendered, judged, and gated profile artifacts
- `artifacts/v1/frames_1fps/` for its own extracted frame timeline
- `artifacts/v4/input_manifest.json` for hashes of every reused v1 input
- `artifacts/v4/fast_scoreboard.jsonl` for the locally observed, two-frame
  corroborated score changes
- feedback port `8092`
- `/tennis_feedback`, `/tennis_rounds`, and `/tennis_trigger`
- `/var/www/html/experiments/tennis_commentator/v4_5s/` and
  `/var/www/html/experiments/tennis_commentator/v4_2s/` for the review pages

The experiment contains isolated 5-second and 2-second fixed-delay
replays, not SRT transport tests. Each profile processes the exact clip
timeline and drops lines that would miss that profile's window. A fixed-layout
5 fps grayscale observer learns legal next score glyphs from the known initial
state, requires two agreeing frames, and exactly matches all eight transitions
from the independent v1 tracker. Its measured worst-case readiness is 0.402s.
Speech is prewarmed before the match from deterministic intent templates; this
is a guarded replay evaluation, not proof of live transport latency. Keeping
it off the football SRT ports and live frame
directories is deliberate; a later real-stream profile can be added only
after this sport-specific grounding is reviewed.

The timing deadline applies to readiness: detector, commentary/localization,
and full TTS synthesis must complete inside the profile budget. Once ready,
the natural spoken duration may extend beyond that budget on the delayed
timeline; no-overlap placement still prevents commentary from clobbering the
next line.

The review page has the same six columns as football: STT, Vision, Tracker,
English, French, Portuguese. “Tracker” is a score-state tracker, not a spatial
player tracker: it accepts only legal, corroborated scoreboard transitions.

Review-page media is deployed with hash-verified hard links when artifacts and
nginx share a filesystem. Rendering writes WAV/MP4 outputs through atomic
replacement, so a future build breaks the artifact link and cannot mutate the
currently served file in place. Page publication then atomically installs the
new verified link. `dedupe_review_media.py` applies the same safe layout to
historical v1/v2/v3 pages.

## Build contract

```bash
cd /home/ubuntu/commentary/experiments/tennis_commentator
./build_v4.sh
```

The build:

1. checks isolation and dependencies;
2. runs fail-closed unit tests and fixtures;
3. validates and hashes the immutable five-minute clip, Deepgram/Whisper STT,
   and vision inputs retained from v1;
4. runs three complete commentary attempts for each delay profile;
5. renders synced EN/FR/PT tracks and applies a separate worst-of-three quality
   gate to each profile;
6. stages both six-column review pages only after both profiles pass; opening
   the review round and Slack announcement remain explicit final steps after
   manual inspection.

The v4 gate requires 13–18 lines, 8 outcome calls, 2 score-derived pressure
calls, 9 server references, zero live-ball calls, a changeover call, a service-context call, no more
than 3 background calls, and no silence above 40 seconds. It recomputes every
score intent and localization, blocks background at high pressure, caps
English line length and audio placement shift, rejects abnormal synthesis
duration, and fails closed on unsupported tiebreak state. The fixture produces
14 lines in both profiles, median 11 words, P90 14, and zero final audio shift;
maximum gaps are 39.8s at 5s and 35.4s at 2s.

After the normal gates, independently verify that the rendered speech matches
the scripts:

```bash
TENNIS_PROFILE=5s /home/ubuntu/commentary/.venv/bin/python audit_rendered_tracks.py
TENNIS_PROFILE=2s /home/ubuntu/commentary/.venv/bin/python audit_rendered_tracks.py
```

If a required check, provider call, track, fixture, or review disposition is
missing, the process exits non-zero. It does not silently publish partial work.

## Review loop

`feedback_server.py` maintains a tennis-only append-only review ledger and
round state. Closing a round writes an actionable digest grouped by profile
and column. Every item must receive an explicit disposition before a later
version may be published. `check_feedback.py` enforces that rule.

Publishing to Slack is an announcement step, not part of page generation:

```bash
SLACK_WEBHOOK_URL=... ./announce_slack.py \
  https://sa-dev.agora.io/experiments/tennis_commentator/v4_5s/ \
  https://sa-dev.agora.io/experiments/tennis_commentator/v4_2s/
```

The command fails closed when no webhook is configured.

If football currently owns the live/model workload, queue the build instead:

```bash
./wait_for_football_and_build.sh
```

The queue polls only process state. It starts tennis after football is idle and
still rechecks isolation before every tennis media/model call. It stages the
passing pages but deliberately stops for manual inspection before review-round
activation or Slack.

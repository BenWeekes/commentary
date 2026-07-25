# Tennis Commentator Pipeline

> Sport-specific fixed-delay commentary experiment for Daniil Glinka vs Aidan
> Mayo at the 2026 Cary Tennis Classic.

## Scope and status

- Code: `experiments/tennis_commentator/`
- Source video:
  `/home/ubuntu/tennis_challenger_atp_challenger_cary_usa_men_singles_8960445_512k.mp4`
- Exact clip: `02:00:15` through `02:05:15` (300 seconds)
- Closed v1 review URLs: `/experiments/tennis_commentator/v1_10s/` and
  `/experiments/tennis_commentator/v1_6s/`
- Live v2 review URLs:
  `/experiments/tennis_commentator/v2_10s/` and
  `/experiments/tennis_commentator/v2_6s/`
- The pipeline contains separately gated 10-second and 6-second fixed-delay
  replay/evaluation profiles, not an SRT transport test.
- Final v1 gate: both profiles PASS on all three attempts, with 12/12 selected
  lines surviving and a 58-second maximum boundary-aware gap.
- v1 review is closed. Both cadence comments were accepted for v2. Both v2
  profiles passed 3/3 with 18/18 lines surviving and a 32-second maximum gap.
- It is deliberately isolated from `experiments/ai_commentator/`.

The pipeline must refuse model/media work while an actual football
`run_blend_live.py`, `run_blend_true_live.py`, detector, or blend frame receiver
is active. Shell watchers that merely contain those strings do not count.

## Verified match context

At clip time zero the graphic reads 0 sets, 0 games, 0 points; Aidan Mayo
serves. Daniil Glinka wears blue and starts at the far end. That end mapping is
not permanent: the players change ends after the first game. Court mapping is
derived from accepted completed games, while scoreboard rows remain tied to
the printed player names.

Pre-match context is cutoff at the start of June 29, 2026. The bundle uses
official ATP, USTA, ITF, and Tennis Canada sources. The match result and any
later information are excluded.

## Architecture

```text
exact five-minute clip
  ├─ broadcast audio ─► Deepgram Nova-3 ─┐
  │                                      ├─ sanity/dedup merge ─► STT column
  │                    Whisper compare ──┘
  │
  └─ 1 fps frames ─► 3-frame bursts ─► conservative vision observer
                                            │
                                            ├─ literal phase/observation
                                            └─ raw named-row scoreboard
                                                      │
                           legal transition + confidence + two reads
                                                      │
                                              accepted score tracker
                                                      │
                         verified context + accepted score + literal vision
                                                      │
                              EN call + FR / pt-BR localizations
                                                      │
                          same ElevenLabs voices as football, isolated tracks
                                                      │
                     separate 10 s / 6 s gates: on-time or dropped, never late
```

The review page exposes the same columns as football: STT, Vision, Tracker,
English, French, Portuguese.

## Scoreboard and identity rules

The compact Cary graphic often omits set values and blanks point values after a
game. A candidate score therefore carries forward missing values from the last
accepted score. That is not enough to commit it:

1. confidence must be at least 0.86;
2. the transition must be exactly one legal tennis point/game/set transition;
3. a changed score needs two consecutive agreeing reads;
4. raw server guesses cannot change server mid-game;
5. a visible game-count change resets points and alternates server.

Skipped points, reversals, inconsistent service changes, and isolated OCR
blips are held in the Tracker column with a rejection reason.

The legacy detector fields `far_*` and `near_*` mean the stable Glinka and Mayo
scoreboard rows, respectively; they do not remain physical court ends after a
changeover. Commentary receives a separate current court map derived from
completed games.

## STT policy

Deepgram and Whisper are retained as independent artifacts. Whisper's first
comparison produced pathological one-second repetition and echoed its own
prompt, so the merge rejects prompt echoes, high-frequency repeated text, and
low-confidence Whisper segments. Rejected segments remain in
`artifacts/v1/stt_rejected.jsonl` for audit.

STT is auxiliary. It appears on the review page with confidence/provider, but
the writer sees only confidence >= 0.82, and STT can never override vision or
the score tracker.

## Commentary and timing

Corroborated score changes use deterministic server-led wording so the core
score cannot be creatively rewritten. The opener and every accepted score
transition identify the accepted server or the player serving next. Sparse,
verified pre-match color fills safe between-point pauses.

Vision-rally calls use a closed set of conservative phrases and require literal
detector text such as “ball in play between the two baselines”, “baseline rally”,
or “ball in play across the net”. They never infer shot type, point winner,
error, tactic, or physical-end identity. The first accepted game also permits
one grounded changeover call and one first-service-game context call.

Each candidate records provider latency. The selected attempt adds real
ElevenLabs latency and no-clobber placement. A line that cannot finish within
its detector-to-complete-TTS readiness budget of 10 or 6 seconds is marked
dropped and is neither voiced nor shown as heard commentary. Natural spoken
duration is not inference latency: after synthesis is ready it may extend
beyond that interval on the delayed timeline, while no-overlap placement still
prevents clobbering. The profiles reuse only immutable input analysis;
commentary, tracker output, audio, judges, and gates live in separate profile
directories.

Three complete attempts are judged independently. Publishing uses a
worst-of-three policy: any missing/malformed artifact, positive hallucination
judge result, missing language, low survival, excessive gap, detector failure,
or failed fixture stops the build.

For v2, every attempt must retain 16–35 lines, at least 9 explicit server calls,
3 literal rally calls, 1 changeover call, and 1 service-context call, with no
boundary-aware gap above 40 seconds. The immutable input fixture produces 18
lines and a 32-second maximum gap. TTS deadline survival is still evaluated
separately for each profile.

Final v2 result:

| Profile | Attempts passing | Selected survival | Server / rally calls | Maximum gap |
|---|---:|---:|---:|---:|
| 10 s | 3/3 | 18/18 | 9 / 4 | 32 s |
| 6 s | 3/3 | 18/18 | 9 / 4 | 32 s |

## Human review lifecycle

The tennis review backend is independent:

- service: `tennis-feedback.service`
- bind: `127.0.0.1:8092`
- routes: `/tennis_feedback`, `/tennis_rounds`, `/tennis_trigger`
- storage: `experiments/tennis_commentator/feedback/`

Feedback is append-only. Closed-round late submissions are rejected but
retained under `late/`. Closing a round creates stable feedback IDs and a work
order grouped by profile/column. `check_feedback.py` requires an exact,
one-to-one set of dispositions, each with status, reason, change, and
verification, before a later version can publish.

Future review-page and Slack release titles use the exact format
`AI Tennis commentator — vX ready for review`.

## Re-run

```bash
cd /home/ubuntu/commentary/experiments/tennis_commentator
./build_v2.sh
```

The build stops if football becomes active between stages. Resume only after
the football worker clears; existing successful artifacts are retained.

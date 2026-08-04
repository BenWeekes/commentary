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
- Preserved v2 comparison URLs:
  `/experiments/tennis_commentator/v2_10s/` and
  `/experiments/tennis_commentator/v2_6s/`
- Live v3 review URLs:
  `/experiments/tennis_commentator/v3_10s/` and
  `/experiments/tennis_commentator/v3_6s/`
- The pipeline contains separately gated 10-second and 6-second fixed-delay
  replay/evaluation profiles, not an SRT transport test.
- Final v1 gate: both profiles PASS on all three attempts, with 12/12 selected
  lines surviving and a 58-second maximum boundary-aware gap.
- v1 review is closed. Both cadence comments were accepted for v2. Both v2
  profiles passed 3/3 with 18/18 lines surviving and a 32-second maximum gap.
- V2 received no submitted comments before the user explicitly directed a v3
  release; its zero-item disposition passed before v3 opened. V3 is the only
  writable round and passes 3/3 in both profiles with 18/18 lines, state-aware
  outcomes/stakes, zero final audio shift, and independent rendered-track STT.
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
                          pure outcome / pressure derivation + policy
                                                      │
                       structured intent + verified context + literal vision
                                                      │
                              EN call + FR / pt-BR localizations
                                                      │
                       tennis EN voice + retained football FR/PT voices
                                      isolated audio tracks
                                                      │
                     separate 10 s / 6 s gates: on-time or dropped, never late
```

The review page exposes the same columns as football: STT, Vision, Tracker,
English, French, Portuguese.

### Disk-safe media deployment

The six v1/v2/v3 review pages remain fully available without keeping a second
physical copy of every MP4. `dedupe_review_media.py` verifies source and
deployment size plus SHA-256, then replaces same-filesystem copies with hard
links. The July 26 cleanup deduplicated 24 files and removed 229.3 MiB of
physical duplication; the preserved gated artifacts remain the canonical
inodes.

This does not let an in-progress render alter a live page. `render_tracks.py`
writes each WAV and MP4 to a temporary sibling and atomically replaces the
artifact, which breaks any old hard link. `build_review_page.py` atomically
installs the new link only after the gate passes. Cross-filesystem deployments
fall back to a normal atomic copy.

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

V3 derives point winner, point streak, pressure count, saved game points,
hold/break, and next server from each corroborated transition before creating
language. Every spoken row retains the previous/current tracker, structured
intent, evidence, state phase, and policy reason. EN, FR, and pt-BR render from
that same intent, and the gate recomputes the transition and exact localized
output. A later graphic set reset is not counted as another played point.
Numeric tiebreak state is explicitly unsupported and fails closed.

Background is state-aware: it is blocked at single game point, break point,
deuce, advantage, set point, match point, and unsupported score state. Multiple
server game points can leave one safe between-point context window after the
stakes have already been stated.

Vision-rally calls use a closed set of conservative phrases and require literal
detector text such as “ball in play between the two baselines”, “baseline rally”,
or “ball in play across the net”. V3 combines that evidence with the last
accepted server, score, or game-point count. Vision still never infers shot
type, point winner, error, tactic, or physical-end identity. The first accepted
game also permits one grounded changeover call and one first-service-game
context call.

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

For v3, every attempt must retain 16–22 lines, at least 8 outcome calls, 4
pressure calls, 9 explicit server calls, 3 literal rally calls, 1 changeover
call, and 1 service-context call, with at most 3 background calls and no
boundary-aware gap above 40 seconds. English median length must be 10–16 words,
P90 at most 18, and every line at most 20. Background/pressure policy,
structured-intent agreement, language rendering, TTS duration, and placement
shift are programmatically checked.

Final v3 result:

| Profile | Attempts | Survival | Outcome / pressure | Server / rally | Median / P90 | Max gap / shift |
|---|---:|---:|---:|---:|---:|---:|
| 10 s | 3/3 | 18/18 | 8 / 4 | 10 / 4 | 11 / 15 words | 32 s / 0 s |
| 6 s | 3/3 | 18/18 | 8 / 4 | 10 / 4 | 11 / 15 words | 32 s / 0 s |

`audit_rendered_tracks.py` independently transcribes the final AI-only WAVs.
All six profile/language tracks pass: aggregate script-to-speech similarity is
0.83–0.91 for 10 s and 0.84–0.92 for 6 s, with every opener heard near 1.7 s
and every final outcome heard by 294.1 s. This audit caught and led to fixes for
one pathological 39–63-second French synthesis, one ambiguous French spoken
score, Portuguese pluralization, and a long rally localization before release.

The English tennis voice was changed after publication to ElevenLabs voice
`kfU9VUUMjY4PWNoUfZ45` without changing the script or URLs. Both profiles were
fully regenerated: 18/18 lines survived, all three gate attempts remained
PASS, maximum audio shift was zero, and the independent English speech
similarity was 0.9108 (10 s) and 0.9157 (6 s). French and pt-BR retain their
prior voice IDs. `render_manifest.json` records the exact voice IDs and
SHA-256 hashes for all six profile media files, and the gate rejects a manifest
whose profile or voice configuration differs from the running code.

## Television commentary reference

The pipeline can access public official full-match recordings without copying
or republishing them. `build_tv_corpus.py` now studies five official WTA
replays in three fixed five-minute windows each:

- four commentary references across Queen's Club grass, Madrid clay, Toronto
  hard court, and Indian Wells hard court — 60 sampled minutes;
- one 15-minute Miami doubles clean-world-feed control, which contains only
  four court/player utterances and no booth commentary.

Two commentary references use official English captions. Two references and
the world-feed control use transient audio-only Deepgram STT. Downloaded
windows are deleted immediately after STT. Source media and transcript text
are never retained.

The safe derived artifact
`benchmarks/tv_corpus_paraphrases.jsonl` is a timestamped surrogate transcript.
Each row keeps only source/window identity, start/end time, word count,
three-way consensus category and function, and a short paraphrase. Paraphrases
are limited to 12 words and checked against every four-word source sequence;
unsafe or missing text becomes a fixed generic function note. Per-source
fingerprints make the run auditable without preserving source content.

Across the four real commentary references:

| Measure | TV references | Glinka–Mayo v2 | Glinka–Mayo v3 |
|---|---:|---:|---:|
| Commentator turns/minute | 3.37 | 3.60 | 3.60 |
| Median words/turn | 17 | 8 | 11 |
| P90 words/turn | 53 | 10 | 15 |
| Speech / AI-voice occupancy | 43.0% | about 16% | not a release gate |
| Longest observed silence | 79.6 s | 32 s | 32 s |

Caption/STT cues within one second are merged, so the TV P90 can include
adjacent contributions from two commentators and is not a generated-line
target. The robust conclusion is that v2 already speaks often enough; its
turns are much shorter and more score-heavy.

Three independent classifiers produced 202 consensus commentator turns:

| Primary category | Turns | Share |
|---|---:|---:|
| Match narrative or stakes | 44 | 21.8% |
| Tactics or pattern | 39 | 19.3% |
| Point reaction or outcome | 36 | 17.8% |
| Technique or shot | 28 | 13.9% |
| Score or server | 23 | 11.4% |
| Player background | 12 | 5.9% |
| Conditions or venue | 10 | 5.0% |
| Banter or other | 10 | 5.0% |

The functional labels reinforce this: explaining the previous point (44),
framing match stakes (42), and explaining tactics/technique (40) dominate;
plain score/server statements account for 22 turns and live-action calls for
17.

V3 implements that conclusion without increasing call count: selected calls
expand toward 10–18 words, accepted score transitions become a known point
outcome plus score significance, and literal rally observations include
accepted server/score context. Tactical, technical, momentum, emotion,
winner/error, and shot-quality claims remain blocked until the detector can
ground them directly. The detailed findings and rerun command are in
`experiments/tennis_commentator/benchmarks/tv_commentary_playbook.md`.

The implemented contract is in
`experiments/tennis_commentator/plan_v3.md`. Its main change is a state-aware
commentary router: accepted point/game/set phase determines commentary type,
while raw elapsed time only limits background-fact frequency.

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
./build_v3.sh
TENNIS_PROFILE=10s /home/ubuntu/commentary/.venv/bin/python audit_rendered_tracks.py
TENNIS_PROFILE=6s /home/ubuntu/commentary/.venv/bin/python audit_rendered_tracks.py
```

The build stops if football becomes active between stages. Resume only after
the football worker clears; existing successful artifacts are retained.

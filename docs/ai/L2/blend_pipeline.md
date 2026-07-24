# AI Live Commentary — Blend Pipeline & Improvement Process (current state)

> Written 2026-07-24 for external review; updated same day after the v7 acceptance.
> Authoritative description of the `experiments/ai_commentator` true-live pipeline and
> the HITL process, as of round **v7 (formally ACCEPTED: worst-of-3 on all three profiles,
> survival 1.0 x 9 runs, all fixtures green — the first formal acceptance)**. Companion docs: [hitl_tuning_workflow.md](hitl_tuning_workflow.md)
> (the process loop in depth), [review_cycle_1_dispositions.md](review_cycle_1_dispositions.md)
> (per-comment audit of cycle 1). History/narrative: [../L1/10_experiments.md](../L1/10_experiments.md).

## What it is

A live AI football commentator. A 5-minute broadcast clip (Mainz vs Union Berlin,
Bundesliga MD33, 2nd half ~76–81', 1-1) is pushed over SRT in real time; the pipeline
watches the video, fuses three signals into one spoken commentary line at a time, voices
it in **EN / FR / pt-BR**, and places every line *on the play it describes* behind a
fixed broadcast delay. Output is reviewed by humans on a web page, feedback is captured
per cell, distilled into generic rules with deterministic regression evals, and the next
version ships only when the evals hold.

## Signal sources

| Signal | Source | Trust level | Availability model |
|---|---|---|---|
| **STT** | Harvested live-Soniox short phrases from the real broadcast audio | Verbatim human truth — always preferred when present | Gated by realistic finalize time: usable at `end_s + STT_LAG` (1.8 s at 10 s profile, 1.5 s at 6 s) |
| **Vision** | Frame bursts (4 × 0.55 s-spaced JPEGs, 960×540) → vision LLM (`gpt-5.6` structured at 10 s, `gpt-5.4-mini` at 6 s), 4 workers, JSON events + possession with confidence tiers | Fallible — the pipeline's main error source | ~5.5 s (structured) / ~2.4 s (mini) per burst |
| **Tracker** | Objective on-pitch positions/shape | Ground truth for location/shape | Near-realtime (`TRK_LAG` 0.5 s) |
| **Pre-match data** | Sportradar lineup (`sr_cache.json`): jersey → name/team/position, kit colours, team naming forms | Authoritative roster — grounds naming and attribution | Static, loaded at start |

## Pipeline stages (`run_blend_true_live.py`, importing `run_blend_live` as B and `run_events_detector` as D)

```
ffmpeg -re (source.mp4) ──SRT──▶ receiver → f_%05d.jpg (0.55s, 960×540)
                                     │ bursts of 4, 4 workers
                                     ▼
                          vision LLM → detections (events/possession + confidence)
                                     │
   STT pool (availability-gated) ────┤          tracker line ────┐
                                     ▼                           ▼
        ┌── (1) verbatim STT phrase preempts (R8 sanity-vetted vs vision events)
        ├── (1.5) R1 priority: high-conf card/goal/penalty preempts pacing
        │        (goal needs R10 corroboration: ≥3 high-conf sightings over ≥5 s)
        └── (2) eager commentator (EAGER_SYSTEM, window of grounded observations)
                 with B.chooser as hedge/fallback; GEN_RULES (R2,R4,R5,R6,R11,R12,R13)
                 appended to BOTH stages
                                     │
                                     ▼
                 enforce_attribution()  ← deterministic R12 guard (see Guards)
                                     │
                                     ▼
        TTS (ElevenLabs flash v2.5) — EN gates placement within the delay budget;
        FR/PT best-effort via localizers (translate_fr / translate_pt + glossaries);
        slow FR/PT = silent-but-logged (missing_tracks), never desyncs EN
                                     │
                                     ▼
        placement at t_det behind the FIXED broadcast delay — drop-late policy:
        a line lands on its play or is never heard; write-order commit drops the
        later line on a stall; audio written into per-language PCM buffers
```

**Sync policy is the core invariant:** every surviving line is spoken about the moment
on screen. Nothing ever slips late. Survival rate (lines that made the window) is a
guarded metric (≥ 0.95); v6 runs measured 0.977 (10 s), **0.90 (6 s — below the
guard; see Open items)**, 1.0 (6 s_vt). Run-to-run variance of ±5 lines is documented;
marginal fails get one rerun, never best-of-N acceptance.

## Profiles & knobs

`SUFFIX = ('_eager') + ('_6s' if 6s) + ('_vt' if no STT) + RUN_TAG` names every artifact,
so variants never clobber each other.

| Profile | Knobs | Vision | Notes |
|---|---|---|---|
| **10 s** | `BLEND_DELAY_S=10` | gpt-5.6 structured | Default quality profile |
| **6 s** | `BLEND_DELAY_S=6` | gpt-5.4-mini + guards | Team claims need tracker agreement; naming high-conf only |
| **6 s_vt** | `BLEND_DELAY_S=6 USE_STT=0` | as 6 s | Vision/tracker-only (no verbatim STT) — reviewer-requested variant |

Other knobs: `BLEND_MODE` (eager default per R9), `RUN_TAG` (version tag, e.g. `_v6`),
`CLIP_ID` (per-clip corpus key), `CLIP_ROSTER` (roster path for the R12 eval).
Runners: `build_v6.sh` (all three profiles end-to-end: live run → mux ×3 langs →
review page → self-check), `build_variant_6s_vt.sh`, `build_v5*.sh`.

## Deterministic guards & evals

Two layers: **prompt rules** (ask the model) and **code guards + evals** (enforce and
verify). The lesson of v5→v6: prompt rules alone are not reliable on the mini model —
anything with ground truth gets a code guard and a fail-closed eval.

**In-pipeline code guards:**
- `enforce_attribution()` (R12): if a card/goal/sub/any line credits a named roster
  player "for/pour \<team\>" and the roster says otherwise, the team reference is
  **corrected** (register-matched form substitution, e.g. "the home side"→"the away
  side") — never stripped, so no commentary is lost. Skips award beneficiaries
  ("free kick for X") and both-teams-named lines. Applied before EN TTS and FR/PT
  translation so all languages inherit the fix.
- R10 goal corroboration, R2 filler code gate (≥15 s genuine silence), R8 STT sanity
  veto (pre-vetted at prewarm), F11 ordered audio writes, 30 s team-agnostic card dedup.

**Regression evals** (`eval_snapshot.py`) — AUTO fixtures are fail-closed (missing/skip
= FAIL) and must pass in **all** runs of a worst-of-N (N≥3) gate; the suite only grows:

| Fixture | Checks |
|---|---|
| R1/R1b | every high-conf card/goal/penalty gets a line ≤8 s; ≤1 card line/30 s |
| R2 | no filler within 15 s of the previous line |
| R3 | repeated fact within 25 s requires new info (a name) |
| R4 | possession flips carry a transition marker |
| R7 | banned French calques never appear (reviewer-extendable glossary) |
| R10 | goal-call lines backed by ≥3 high-conf detections spanning ≥5 s |
| R11 | team-reference variety from approved pre-match forms |
| R12 | no line credits a team-specific event to a named player of the other team (roster-resolved; award-beneficiary + ambiguity exclusions) |
| R13 | no camera language ("in the frame/shot", "dans le cadre" (non-idiomatic), "na tela") in any language |
| R5/R6/R8 | reviewer-checked (no deterministic oracle) |

Guarded metrics on compare: hallucinations ≤ baseline, survival ≥ 0.95, desync = 0,
first line ≤ 2 s. Thresholds change only via ledger amendment — never inline.

## Review & feedback process

- **Pages** (`build_hybrid_page.py` → `/blend_<version>_<profile>/`): video + 6-column
  timeline (STT / Vision / Tracker / EN / FR / PT), sidebar explaining the review model
  (EN = the commentary; FR/PT = the *translation*; STT/Vision/Tracker = the inputs) and
  the fixed-delay note. Click a cell → tagged comment (📝 editable until Submit, Esc
  closes, 👍 one-click); auto-scroll suspends while a box is open.
- **Capture** (`submit_server.py`, 127.0.0.1:8091 behind nginx, systemd
  `blend-feedback.service`): every comment stores `(clip, version, profile, t, column,
  cell_text, tags, comment)` — append-only `feedback/<round>/comments.jsonl`,
  git-committed, **never truncated**. Round state machine (`rounds.json`); late posts →
  409 + archived under `late/`. PIN-guarded trigger closes a round and writes a work
  order **digested by (profile, column) with build-side routing** (`digest_round()`:
  EN→content rules, FR/PT→localizers, inputs→detector/tracker) so distillation starts
  from an actionable grouping, not a raw dump. Surrogate-scrubbing + catch-all error
  handling (a lone half-emoji once 502'd a reviewer's submission).
- **Corpus** (`clips.yaml`): every reviewed clip becomes permanent fixtures; the gate
  re-runs **all** clips on every candidate. Currently one clip (see Open items).
- **Loop**: Review → Distill (generic rules only; pin-the-defect first) → Implement
  (one at a time) → Gate (worst-of-N + all fixtures) → Accept/Reject (rejections kept
  with measured reasons) → rerun → re-review. New-build links are announced via Slack
  webhook (links + honest scope notes only).

## Version history & measured outcomes

| Round | Shipped | Measured outcome |
|---|---|---|
| v4 (2026-07-22) | 3 profiles (10s/6s/6s_vt), PT track, on-page feedback | Alex: 141 comments; error clusters = FR phrasing (largest), team attribution, camera lines |
| v5 (2026-07-23) | R12 attribution (prompt + narrow guard), R13 camera ban, R7 FR localizer upgrade | Alex: 159 comments, **71% 👍 across all three profiles**; camera lines 0; attribution leaked on non-card lines (guard too narrow) |
| v6 (2026-07-24) | Attribution guard broadened to any "player for \<wrong team\>" line, corrective not destructive | Offline vs Alex's v5 lines: **2/3 flagged attribution errors resolved, 0/50 👍-good lines altered, touches 2/81 lines total**; all fixtures green live on all three profiles |

## Current quality state — the honest picture

Of Alex's ~43 distinct v5 actionable issues: **2 fixed by v6** (attribution), **~21 are
French phrasing refinements** (addressable via R7 glossary growth — largest remaining
addressable bucket), **~14 are perception-layer** and *not fixable by rules*:
wrong player identity (vision misread: "Tietz" for "Sieb"), territory direction
("their own third" vs the opponent's), and team-only event lines with no player to
anchor the roster on. One clock-drift item and one localizer-term overuse
("trente derniers mètres" repeated + mispronounced by TTS) are queued.

**Reliability thesis (open decision):** deterministic error classes are provably
closable (guard + eval, demonstrated twice). Perception errors bound the ceiling. The
proposed next step is a **reliability mode** — name a player only at high-confidence
identity, spatial claims only from tracker, vision as corroboration — measured by hard
error count on the same clip; that number decides ship-vs-park. Alternative levers:
better vision models (a Claude Opus 4.8 vision+chooser A/B is prepped, blocked on an
API key), and STT with roster-biased custom vocabulary (Gemini 3.5 Transcribe EAP;
blocked on an allowlisted-project key — our key 404s on the preview models).

## Open items (for review)

1. **R12/R13 status**: implemented + live-validated on single runs per profile, but not
   yet through a formal worst-of-N acceptance trio → still `implemented-pending-gate`
   in the ledger.
2. **Profile mismatch not gate-enforced**: `fixed_delay_s`/vision model are recorded in
   snapshots but compare does not reject a cross-profile comparison (ledger amendment
   required by convention instead).
3. **Single-clip corpus**: all rules are distilled and gated on one 5-minute dev clip.
   The declared holdout discipline (next reviewed match) is still pending a second clip.
4. **Text judge saturates** (2/5 with the real booth at 3/5) — used as a regression
   tripwire only, never a target.
5. **Perception ceiling** (above) — reliability-mode measurement not yet run.
6. **v6 6 s run shipped at survival 0.90 — below the 0.95 guard.** The build runner's
   self-check verified fixtures but not the guarded survival metric (a runner gap, now
   documented); per the variance policy this warrants a rerun before the round closes.
   FR/PT missing-track rates (4/1–2 per run) are logged but have no guard threshold.
7. **Ops**: feedback server is same-origin behind nginx with a low-entropy PIN
   (ordering guard, not a secret); production `status_api` exposure previously flagged
   is out of this experiment's scope.

# Human-in-the-Loop Tuning Workflow — AI Live Commentary

**Purpose:** turn reviewer feedback into *generic* rules and gates so that every rerun
of the pipeline produces a measurably better outcome **without regressing** what already
works. This is the standing improvement process for the blended commentary system;
it improves quality with the *current* inputs (STT + vision + tracker) — it is not the
vehicle for new capabilities (e.g. ReID identity), which remain separate projects.

Last reviewed: 2026-07-22.

---

## On-page feedback system (v4, 2026-07-22) — Step 1 is now in the browser

Reviewers no longer use a spreadsheet. Feedback is captured **on the results page itself**:

- **Cell-level comments** — click any cell → inline box with a free-text comment and quick
  tags (`wrong fact · repetition · language · naming · timing · 👍 good`). Each pins to
  `(clip, version, profile, row-time, column)` plus a snapshot of the cell text, so it
  survives into the next version and can be diffed against what replaced it. 👍 marks protect
  good lines from over-correction.
  - **`profile`** (`6s` / `10s`) records *which delay page* the comment was made on — the two
    pages are reviewed in parallel, so without it a comment is untraceable. **`clip`** keys the
    comment to its match segment for the per-clip corpus (below).
  - **Editable before submit** — a commented cell shows a 📝 icon; click it again to edit or
    **Remove** the pending comment. After Submit the icon turns to a green ✓. Nothing is sent
    until you press Submit.
  - **No scroll-fighting** — auto-follow freezes while a comment box is open, and any manual
    scroll (wheel/touch) pauses auto-follow for 6 s, so you can read and comment without the
    playhead yanking the grid.
- **Bottom bar** — reviewer name (remembered in the browser), an unsent-comment counter, and
  two deliberately-separated buttons:
  - **Submit feedback** — any reviewer, any number of times, while the round is open.
  - **Close round & trigger next version** — PIN-guarded, pressed by **one** operator; it
    freezes the round and writes `feedback/trigger_<version>.json`, the work order for the
    next cycle. It does **not** run the pipeline (that stays human-initiated).

**Backend** (`submit_server.py`, 127.0.0.1:8091 behind nginx same-origin; routes
`/blend_feedback`, `/blend_rounds`, `/blend_trigger`):
- `feedback/rounds.json` is the round state machine (`{current, rounds:{v: {status}}}`).
- Comments append to `feedback/<version>/comments.jsonl` (git-committed each round — the
  feedback history now lives beside the rules it produced).
- **Late-comment policy:** once the next round opens, posts to a closed round return 409 and
  are archived under `feedback/<version>/late/` marked rejected — *not destroyed*; a good
  late observation can be manually promoted. Rejection is about process ordering, not
  information loss.
- **Feedback is never deleted or truncated.** `comments.jsonl` is append-only and committed
  to git per round — it is the source material for the clip's permanent test cases. Do not
  `>`-truncate it; to drop a stray test entry, filter that one line out, never blank the file.

**Column → build target routing** (what "How to review" on the page means downstream). On
trigger, `submit_server.digest_round()` groups the round's comments by `(profile, column)` and
stamps each group with `routes_to`, so the work order (`trigger_<version>.json`) is actionable
along the same axes reviewers used — not just a raw comment dump:

| Column | Reviewer reviews | Routes to (build side) |
|---|---|---|
| English | the commentary itself | chooser/writer prompt + content rules (R1–R6, R9, R10) |
| French | the *translation* | football-French localizer + glossary (R7) |
| Portuguese | the *translation* | Brazilian localizer + glossary |
| STT | the input signal | ASR sanity/veto (R8); rarely a rule target |
| Vision | the input signal | detector/vision prompt (perception limits are roadmap, not rules) |
| Tracker | the input signal | tracker corroboration (R10); positions are objective |

`profile` scopes a change to the 6s or 10s pipeline — a 6s-only timing complaint must not mutate
the 10s build. The digest keeps `positive` (👍 good) counts so good lines are protected, not
"fixed".

**Per-clip regression corpus** (`clips.yaml`): every reviewed clip becomes a permanent test
case. Its distilled feedback yields generic rules + deterministic fixtures tagged with the
clip id, and `eval_snapshot.py` re-runs **all** clips' fixtures on every candidate build — a
fix for one clip can never silently regress another. Add a clip by giving it an id in
`clips.yaml` and running the pipeline with `CLIP_ID=<id>`; clips are never removed.
- **Security** (post-Codex-review hardening): path-traversal-safe identifiers (no `.`), a
  256 KB body cap, per-field length limits, constant-time PIN compare, atomic `rounds.json`
  writes under a lock, and `version == current` enforcement on trigger. The endpoints are
  internet-reachable, so treat the PIN as low-assurance (throttled, not brute-proof) —
  it gates *ordering*, not secrets.

**Runtime & operations:**
- The backend runs as a **systemd service** `blend-feedback.service`
  (`ExecStart=.venv/bin/python submit_server.py`, `Restart=always`, `WantedBy=multi-user.target`)
  — it survives reboots and auto-restarts if killed (verified: kill -9 → back in <3 s).
  Manage with `systemctl status|restart blend-feedback`; logs at `/var/log/blend-feedback.log`.
- **Trigger notification:** pressing "Close round" only writes `feedback/trigger_<version>.json`
  — nothing runs the next cycle automatically. A file-watcher (`until ls feedback/trigger_*.json`)
  is used to alert the operator when a round closes; the operator then starts the next cycle
  by hand. The pipeline is never launched unattended.
- **Endpoint contract** (all same-origin via nginx): `GET /blend_rounds` → round state;
  `POST /blend_feedback {reviewer,version,items[]}` → 200 stored / 409 if round closed
  (archived under `late/`); `POST /blend_trigger {version,pin,triggered_by}` → 403 wrong PIN,
  409 unless `version==current` and open, 200 closes the round and writes the work order.

**Roles:** the work (rule implementation + gating) is done on this box → **Codex CLI reviews**
the change (`codex exec --sandbox read-only …`, see the review recipe at the end) → the URLs
are shared in **Slack** for the three reviewers (Alex EN, Tiago PT, plus you) to comment on
the page → one person triggers → repeat.

## Languages & delay profiles (v4)

- **Three languages** now render per line: **English**, **French** (football-French
  localizer + R7 glossary), **Brazilian Portuguese** (`translate_pt` localizer + its own
  seeded glossary + the production voice `HR2TRGmi4QbMsO5omv7l`). PT comments feed the PT
  glossary exactly as Alex's fed the French one — Tiago owns that lane.
- **Delay is a deployment knob** (`BLEND_DELAY_S`). Two published profiles:
  - **10 s** (default): gpt-5.6 structured vision (~5.5 s), richest detection.
  - **6 s fast profile** (`BUFFER_S ≤ 7`): mini-structured vision (~2.4 s, every burst
    analyzed) with extra guards for mini's weaker calls — team claims require **tracker
    agreement** (else location-only), player-naming **high-confidence only**, tighter STT
    finalize window (some long phrases drop), single fast menu chooser (the hedge is wasted
    at 6 s), STT-slot protection. More lines, faster reactions, plainer reads.
- ⚠️ **Gate note (open item):** `fixed_delay_s`, vision model, and profile are recorded in
  each snapshot but the gate does **not yet reject** a candidate compared across a different
  profile. Until it does, a profile change requires a ledger amendment. Comparing a 6 s
  candidate against a 10 s baseline is **not** a valid gate.

---

## The loop at a glance

```
   ┌────────────┐    ┌──────────────┐    ┌─────────────────┐    ┌──────────────┐
   │ 1. REVIEW  │───▶│ 2. DISTILL   │───▶│ 3. IMPLEMENT     │───▶│ 4. GATE      │
   │ humans mark│    │ issues into  │    │ each rule as a   │    │ rerun + eval │
   │ lines on   │    │ GENERIC rules│    │ prompt clause or │    │ snapshot vs  │
   │ the page / │    │ (never clip- │    │ code gate        │    │ baseline     │
   │ sheet      │    │ specific)    │    │ (one at a time)  │    │              │
   └────────────┘    └──────────────┘    └─────────────────┘    └──────┬───────┘
         ▲                                                    ACCEPT / REJECT
         │                                                             │
         └──────────────── 5. RERUN + RE-REVIEW ◀──────────────────────┘
```

Artifacts, all versioned in git:

| Artifact | Where | Role |
|---|---|---|
| Reviewer feedback | Google Sheet (Time / columns / comment) or the page tick UI | Raw input |
| **Rules ledger** | `experiments/ai_commentator/tuning_rules.yaml` | Every rule: source, generic statement, implementation, status |
| **Eval gate** | `experiments/ai_commentator/eval_snapshot.py` | Snapshot + compare → ACCEPT/REJECT |
| Baseline snapshot | `experiments/ai_commentator/baseline_eager.json` | The bar every candidate must clear |
| Results page | `https://sa-dev.agora.io/experiments/ai_commentator/blend/` | What reviewers mark |

---

## Step 1 — Review

Reviewers watch the page (audio + 7 columns) and mark issues per timestamped line —
a sheet column or the tick UI. Anything is valid input: factual errors, pointless
lines, repetition, bad translation, missed events, preference between safe/eager.
Positive marks matter too ("very good") — they protect lines from over-correction.

## Step 2 — Distill: issue → GENERIC rule

**The core discipline.** A reviewer comment is about one moment; the rule must be
about a *class* of moments. Test: *"would this rule have prevented the issue AND
does it read sensibly for a match we haven't seen?"*

| Reviewer said (one moment) | ❌ Patch (rejected form) | ✅ Generic rule |
|---|---|---|
| "Why no mention of the yellow card?" (0:06) | "Mention the 0:06 yellow card" | HIGH-confidence card/goal/penalty events always preempt pacing — never skipped (R1) |
| "Safe sentence means nothing" (1:10) | Delete that line | Every line needs ≥1 concrete new fact; filler only after ≥20 s silence, ≤1/60 s (R2) |
| "'sonder' is not football French" (2:38) | Fix that word | FR is football-French *localization* with a versioned glossary, not translation (R7) |
| "substitution, not 'change of foot'" (3:20) | Fix that phrase | STT phrases are vetted against concurrent high-conf vision events; ASR nonsense never propagates (R8) |

Each distilled rule enters `tuning_rules.yaml` as `status: candidate` with its source
comment(s) quoted. Cluster related comments under one rule (six French comments → one
localization rule + glossary).

## Step 3 — Implement, one rule at a time

Two implementation types, chosen per rule:

- **Prompt clause** — added to the relevant stage's system prompt (safe chooser,
  eager commentator, FR localizer). For style/precision/clarity rules (R2, R4–R7).
- **Code gate** — deterministic check in `run_blend_true_live.py` (like the existing
  drop-late, desync-guard and placement gates). For rules that must *never* be left
  to model judgement (R1 event priority, R3 fact-dedup window, R8 STT/vision conflict).

Implement **one rule per gated run** (or one tight cluster like the FR glossary).
Batching several rules makes a regression unattributable.

## Step 4 — Gate: the regression check

Every candidate rule must clear the gate before acceptance:

```bash
cd experiments/ai_commentator
# baseline exists from the last accepted state:
#   .venv/bin/python eval_snapshot.py snapshot commentary_blend_live_eager.jsonl > baseline_eager.json

# 1. rerun with the candidate rule enabled (live over SRT, ~7 min)
BLEND_MODE=eager ../../.venv/bin/python -u run_blend_true_live.py

# 2. snapshot + compare
../../.venv/bin/python eval_snapshot.py snapshot commentary_blend_live_eager.jsonl > cand.json
../../.venv/bin/python eval_snapshot.py compare baseline_eager.json cand.json
```

**Guarded metrics — ANY failure ⇒ REJECT (or fix and re-gate):**

| Metric | Gate | Why it's guarded |
|---|---|---|
| Frame-audited hallucinations | ≤ baseline (target 0) | Accuracy is the product's identity |
| Line survival at fixed 10 s delay | ≥ 0.95 | Coverage floor; sync-drops must not grow |
| Desync shifts > 1.5 s | = 0 | Sync guarantee is structural |
| First line | ≤ 2 s | The opening-silence fix must not regress |

**Watched metrics — reported, human-judged, no hard gate:** words (±15%),
gaps ≥15 s, named-player lines, judge realism/variety (note: the text judge
**saturates at 2/5** — the real booth scores 3/5 on the same rubric — so treat it as
a tripwire for regressions, not a target to optimize).

**Run-to-run variance:** live vision latency varies between runs (±5 lines observed).
If a candidate fails a gate *marginally* (e.g. survival 0.94), rerun once before
rejecting — but never accept on a best-of-N cherry-pick; accept on the *typical* run.

## Step 5 — Accept / Reject, then rerun and re-review

- **ACCEPT** → `status: accepted` in the ledger, commit rule + implementation +
  new `baseline_eager.json` together (conventional commit, e.g.
  `feat: accept tuning rule R3 fact-dedup window`). The new baseline becomes the bar.
- **REJECT** → `status: rejected` with the measured reason kept in the ledger
  (e.g. *"R-x widened lull filler; words +40% but survival fell to 0.88 — the extra
  lines collided with STT slots"*). Rejected rules are knowledge, not failures:
  they document a real trade-off the next person shouldn't rediscover.
- Publish the rerun to the page (`mux_with_crowd.py` ×2 + `build_hybrid_page.py`),
  reviewers mark the new output, loop continues. **Cadence:** batch a review sheet →
  distill all → gate rules one at a time → one re-review pass. Expect 3–6 rules per
  cycle to survive the gate.

### Worked rejection example (hypothetical)

R2 (content floor) implemented as "never emit a line without a named entity" — gate
run shows hallucinations still 0 but survival 0.91 and words −30%: the commentator
NO_CALLs through every lull, then bursts. REJECT this *form*; re-implement as the
rate-limited version (filler ≤1/60 s) — gate passes → ACCEPT. Same reviewer intent,
two implementations, the gate chose between them.

---

## Current ledger state (from review sheet, 2026-07-20 — reviewer: Alex)

21 comments distilled into **9 generic rules** — see `tuning_rules.yaml` for full text:

| ID | Category | One-line rule | Status |
|---|---|---|---|
| R1 | event-priority | High-conf cards/goals/penalties always narrated, preempt pacing | candidate |
| R2 | content-floor | Every line ≥1 concrete new fact; filler rate-limited | candidate |
| R3 | fact-dedup | Same fact ≤1×/25 s unless new info added | candidate |
| R4 | continuity | State reversals must mark the transition | candidate |
| R5 | referential-clarity | No pronouns without antecedent | candidate |
| R6 | precision-restraint | Never state action *manner* the detector didn't provide | candidate |
| R7 | localization | FR = football-French localization + versioned glossary | candidate |
| R8 | stt-sanity | STT phrases vetted against conflicting high-conf vision events | candidate |
| R9 | product-default | **Eager is the default voice** (4 explicit reviewer preferences, 0 for safe) | **accepted** |

R9 needed no gate run: it is a direct human A/B verdict and the hedge already
guarantees the safe coverage floor inside eager.

---

## Process upgrades (adopted 2026-07-20, from the cards-process review)

Five disciplines adopted after an external review of this workflow against a sibling
HITL process ("cards") — one of them validated empirically the same day:

1. **Pin the defect before fixing it.** Between Distill and Implement: each candidate
   rule must cite a pinned fixture — a timestamp + machine-checkable condition that
   reproduces the issue in the *baseline artifact* (expected-fail). Only then is the
   rule implemented. *Empirical validation: the first R1 gate run ACCEPTed while the
   yellow card was still unnarrated — the global gates never encoded the defect. A
   pinned expected-fail ("baseline detections contain a high-conf card with no line
   within 8 s") would have caught it pre-implementation.*
2. **Per-rule permanent fixtures.** Global metrics can't see a repetition rule regress.
   On acceptance, every rule keeps a named regression check in
   `eval_snapshot.run_fixtures()` (R1/R2/R3/R4/R7 automated; R5/R6/R8
   reviewer-checked). **The fixture suite only ever grows** — every future gate re-runs
   all of it.
3. **Gate on the distribution, not one run.** Live runs vary (±5 lines observed).
   Acceptance decisions use **worst-of-N (N≥3)** live runs for guarded metrics —
   `eval_snapshot.py compare baseline.json c1.json c2.json c3.json` — fixtures must
   pass in ALL runs, and the spread is recorded in the ledger. Single runs remain fine
   for exploration; never for acceptance.
4. **Dataset honesty / holdout.** The reviewed match is a *development set*: rules
   distilled and gated on it will overfit to it however generic they read. The next
   reviewed match is reserved as a **holdout** — the full accepted-rule suite re-runs
   on it before any customer milestone. (Pending until a second match is reviewed.)
5. **No silent threshold relaxation.** The guarded thresholds may only change via an
   **amendment entry** in `tuning_rules.yaml` (before-evidence, change,
   after-evidence, commit) — never an inline edit during a difficult rule.

## Scope boundaries (what this loop is NOT)

- **Not for new capabilities.** "Name more players" is bounded by the identity
  ceiling — that's the ReID tracker project, not a rule. A rule that tries to prompt
  identity into existence will fail the hallucination gate.
- **Not for latency work.** The 10 s delay and drop policy are proven infrastructure;
  changing them is an engineering decision with its own measurement, not a tuning rule.
- **Not judge-driven.** The saturated text judge never *accepts* a rule; only the
  guarded metrics + human re-review do.

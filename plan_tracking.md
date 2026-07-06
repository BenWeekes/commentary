# Plan — Football Player + Ball Tracking as Vision-LLM Grounding

## Goal

Add a per-frame **structured tracking layer** — player bounding boxes, kit colours, best-guess jersey numbers, ball position — that runs alongside the vision-LLM in the AI commentator pipeline. The tracker's output is injected as a **text block into the LLM prompt** so the LLM has grounded facts rather than having to identify players from small pixel patches.

Goals in one line: cut hallucinations from ~15 % (current live gpt-5.5 + playerist) to **≤ 5 %** while preserving Pipeline 1's ~8 s fixed delay and Pipeline 2's ~4 s fixed delay.

This is an experiment separate from the currently-shipping commentator; if it works we fold it in.

## What this does NOT try to solve

- Not replacing the vision LLM. The LLM still writes commentary — the tracker just tells it *what's actually in front of it*.
- Not real-time player-of-the-match statistics (would need multi-camera + calibration).
- Not offside detection (needs precise field geometry).
- Not replacing the LLM judge — trackers add signal to the *producer*, not the scoring pass.

## Motivation

From our leaderboard (see `docs/ai/L1/10_experiments.md`):

- gpt-5.4-mini + Gemini + arbiter + strict rules: 16 % hallu (little improvement over single-model 17 %)
- gpt-5.5 + playerist: 8 % offline, ~15 % live — this is our current floor
- 4 of 11 sample-analysed hallucinations were **correlated across models** — both said the same wrong thing. Even a hybrid can't catch these.
- The remaining hallucinations are **identification** ("Amiri" for Kohr) or **event fabrication** ("Klaus punches clear" when he's just holding it).

Vision-LLMs guessing at 20-pixel-tall jersey numbers isn't a solved problem. A dedicated detector + OCR pipeline is a solved problem. This plan closes that gap.

## Success criteria

| Metric | Current Pipeline 1 live | Target with tracker |
|---|---|---|
| Judge hallucination rate | 15 % | ≤ 5 % |
| Player-name accuracy on named-player lines | not measured (est. 75 %) | ≥ 90 % |
| Coverage (fraction of Soniox turns matched within ±5 s) | 97 % | ≥ 97 % (don't regress) |
| Fixed broadcast delay | 8 s | ≤ 8 s (don't grow) |
| Wall-time cost per 5-min slice | ~$1.20 (gpt-5.5 heavy) | ≤ +25 % |

## Architecture

```
                        ┌── ball detector ─────┐
video frame  ──────────►┤                      │
(0.55 s cadence)        ├── player detector ───┤
                        │                      ├──► frame-level JSON
                        └── kit classifier ────┤        ↓
                                               │   {ball_zone, players_by_side,
                                               │    numbers_seen, formation_hint}
                                               │        ↓
                              cross-frame tracker (ByteTrack or DeepSORT)
                                               │        ↓
                                               │   stable IDs across frames,
                                               │   "player 7 has had ball
                                               │    5.2 s this half"
                                               │        ↓
                        ┌──────────────────────┴────────┐
                        │  structured text block:       │
                        │  "Frame at 187 s:             │
                        │   ball in Union final third,  │
                        │   right side.                 │
                        │   #23 (red) carrying ball,    │
                        │   #4 (white) closing.         │
                        │   Formation: reds 2-3-5,      │
                        │   whites 4-4-2 defensive."    │
                        └──────────────┬────────────────┘
                                       │
                    injected into vision-LLM prompt
                    as pre-game rich context section
```

The tracker runs in a **separate process/thread**, parallel to the frame extractor and vision LLM. It never blocks the LLM — worst case its output is stale by one burst and the LLM sees text from the previous burst's frame.

## Approach — three tiers

We land the cheapest tier first, measure hallucination reduction, decide whether to invest in the next.

### Tier A — off-the-shelf detector, no training (target 1-2 days)

**Recommended starting point (2026): `roboflow/sports`** — https://github.com/roboflow/sports

Actively maintained open-source pipeline that bundles ball detection, player detection, kit-colour clustering, jersey-number OCR, homography, and tracker plumbing in one repo. Uses ultralytics YOLO under the hood. Comes with pretrained weights that work reasonably well on football broadcast footage out of the box. Zero training required for milestone 1.

Fallback / from-scratch equivalent stack (assemble manually if `roboflow/sports` doesn't fit):
- `ultralytics` (YOLOv8-nano/small or newer — YOLOv11 was current at time of writing; check for a newer release when starting), pretrained on COCO includes "person" and "sports ball"
- OpenCV for jersey-number crop + Tesseract (or PaddleOCR) for digit recognition
- BoT-SORT (preferred, better re-ID for football's frequent occlusions) or ByteTrack — both bundled with ultralytics — for cross-frame association
- Kit-colour classification via HSV histogram of each person bbox (2-cluster k-means → home vs away)

**Runs on:**
- **New GPU box (3.9.234.40):** ~15-25 ms/frame for YOLOv11n at 960×540, negligible cost at 0.55 s cadence
- CPU inference (this box has 16 cores, no GPU). YOLOv8n on 960×540 frames ≈ 40-80 ms/frame CPU. At 0.55 s cadence, plenty of headroom.
- Cloud alternative: Roboflow Universe hosted inference API — a pretrained football detector is available; ~$0.001/inference; adds ~100-200 ms.

**Output injected into LLM prompt** (one line per frame in the burst, plus one "phase summary"):

```
TRACKING (from external detector — high confidence facts):
  t=188.10s  ball at (right, attacking-third for red team)
             red bboxes: 6 (one on the ball, ~25 px number "23" visible)
             white bboxes: 5 (defensive block)
  t=188.65s  ball still at same position; red #23 moved 3 px right
  t=189.20s  ball moved into penalty area (right); red #23 still carrying
  PHASE: red-team attack sustained for 8.2 s in Union third.
```

The LLM prompt adds: *"Names given below the TRACKING block are high-confidence. If TRACKING says '#23', that IS Becker for Mainz. Do not override tracking-provided identities with your own guesses."*

**Expected hallucination cut:**
- Named-player accuracy jumps meaningfully — "Klaus" instead of "the goalkeeper" is grounded
- Event-verb claims still LLM-generated; those need Tier B/C

**Risks / limitations:**
- Jersey number OCR on 15-25 px digits is unreliable. Expect 30-50 % of numbers unreadable, 10-15 % misread. Fine — the tracker also emits kit colour and rough position which are still useful when OCR fails.
- COCO's "person" class isn't football-specific — coaches, refs, ball-boys will get bboxes too. Handle via bbox-size filter and "on pitch" mask (approximate via green-pixel dominance).
- No goal net / linesman detection — those are harder.

### Tier B — trained football detector (target +1 week)

**Stack:**
- `ultralytics` fine-tuned YOLO (YOLOv11n / YOLOv11s recommended; drop in a newer base model if one has shipped) on football-specific datasets
- Same tracker + OCR from Tier A (BoT-SORT / ByteTrack)
- Adds: **ball-in-play detector** (is the ball actually in play?), **referee bbox class** (ref shirt colour), **fourth-official board detector**, optional **jersey-number classification head** trained on SoccerNet jersey annotations

**Trains on:**
Recency matters — older SoccerNet splits are limited; use the most recent tracks:
- **SoccerNet-Tracking-2024** (or newer if the annual challenge has released a fresher split) — ~12 full-match tracks with player + ball annotations, freely downloadable after signing the licence at soccer-net.org
- **SoccerNet-ReID** — for training a jersey-number classifier / player re-identification head
- **SoccerNet-Jersey** — jersey-number annotations
- **Roboflow Universe "football-players-detection"** family — community-uploaded fine-tuning datasets, quality varies; useful as top-up data
- Also worth checking the latest **SoccerNet MOT Challenge leaderboard** on Papers with Code — winning configs are usually published and give a strong starting recipe

Compute:
- ~4-6 hours of GPU time on a single L4 / A10 (or the new GPU box) for a nano/small variant
- No need for our specific match footage in the training set
- Cost on the new box: negligible (already have the hardware); if renting, ~$5 of GPU rental

**Output additions:**

```
TRACKING (extended):
  ball_in_play: True (or "stopped for injury", "in stands", etc.)
  4th_official_board_visible: True — red 20 / green 44
  card_gesture_visible: none
  ref_position: near midfield
```

The 4th-official board detector is high-leverage — it's the ground truth for substitutions (we've seen the vision LLM miss subs several times). A trained detector for the LED board is straightforward and would eliminate one class of the largest hallucinations.

### Tier C — pose + gesture recognition (target +1-2 weeks)

**Stack:**
- MediaPipe Pose (per-person body-joint keypoints) — CPU-friendly
- Simple classifier on pose keypoints for football-specific gestures:
  - **kicking** (specific leg trajectory)
  - **heading** (torso + neck arc)
  - **raising arms** (celebration or foul claim)
  - **hands-on-head** (dejection)
  - **treatment** (player prone, others gathered)

**Output additions:**

```
POSES (last 2.2 s):
  #23 (red): kicking motion detected at t=188.4 s → likely shot or pass
  #4 (white): arms raised at t=188.1 s → likely appealing
  crowd: three players around a prone figure at pitch centre → likely injury/foul
```

This is the layer that would let the LLM confidently say "Amiri whips it in!" only when there's a matching kicking pose. Without this the LLM guesses from context; with this, it's grounded.

## Milestones

| # | Milestone | Effort | Success signal |
|---|---|---|---|
| 1 | Tier A prototype: YOLOv8n bbox + kit clustering on stored 5-min slice; emit JSON per frame | 1 day | JSON file with 542 frames, spot-check 10 samples look sane |
| 2 | Inject Tier A JSON into gpt-5.5 + playerist prompt; re-run offline batch; score + judge | 0.5 day | judge hallu ≤ 10 % offline (from 8 % → we want NO regression, aim for improvement) |
| 3 | Tier A live: wire tracker as async subprocess in `live_srt_run.py`; run live SRT session | 1 day | live judge hallu ≤ 12 % (from current 15 %); pipeline p90 lag ≤ 8 s |
| 4 | Jersey-number OCR (Tesseract or PaddleOCR); add to Tier A output | 1 day | 60 % of visible numbers read correctly (spot-check); named-player accuracy up |
| 5 | Tier B: fine-tune YOLO on SoccerNet, add ball-in-play + 4th-official-board classes | 3-5 days | 4th-official-board detected in every real sub in the eval slice (currently 3 subs missed by pipelines) |
| 6 | Tier C: pose keypoints via MediaPipe, gesture classifier | 1 week | kicking / heading / arms-raised detected reliably enough to gate event verbs |
| 7 | Live production integration: single frozen "tracker daemon", stable interface to both pipelines | 1 week | drop-in — pipelines don't know if tracker is on/off, degrades gracefully |

Ship after milestone 3 or 5 depending on how big the win is at each step. Milestone 7 only if the whole thing has cleared the quality bar.

## Risks

- **CPU-only inference may be too slow for larger models.** Mitigation: rent a small GPU instance for the tracker daemon (T4 or L4 is enough for YOLOv8-small at broadcast rates). Cost is ~$0.30/hour, negligible for demo, meaningful for 90-min live match.
- **Jersey OCR is inherently unreliable at broadcast resolution.** Mitigation: don't rely on it exclusively — the tracker's job is to give the LLM *high-confidence* signals; low-confidence numbers get omitted, not guessed.
- **Correlated errors between tracker + LLM.** Less likely than model-model correlation because the failure modes are different (detector fails on occlusion, LLM fails on interpretation).
- **Tracker latency growth.** If Tier B/C add > 200 ms/frame we start eating into the pipeline budget. Mitigation: tracker runs one frame ahead of the LLM (frame extraction is already ahead of the vision-LLM's booth-busy gate).
- **Doesn't help if the source video is bad quality.** Fair. On a broadcast feed the resolution is fine; on a phone camera it wouldn't be.

## Cost estimate

| Item | Development cost (one-time) | Runtime cost (per 5-min live slice) |
|---|---|---|
| Tier A (off-the-shelf YOLO + OCR) | 2-3 days engineering | ~$0.02 CPU time OR ~$0.15 Roboflow API |
| Tier B (fine-tuned YOLO + 4th-official-board) | 1 week engineering + ~$5 GPU training | ~$0.05 GPU inference |
| Tier C (pose + gestures) | 1-2 weeks | ~$0.10 GPU inference |
| Full stack (A + B + C in production) | ~1 month total | ~$0.15 per 5 min live |

Total additional runtime cost per live match (~2 hours) is bounded around **$3-4**. This is dwarfed by the LLM commentary cost (~$25/match at Pipeline 1 rates).

## What I'd build first (my recommendation)

**Just Tier A milestone 1-2** — clone `roboflow/sports`, run its football pipeline over the stored `frames/` dir, inject the per-frame detections as a text block into the vision-LLM prompt. No custom training. Two days of work.

If the judge's hallucination rate drops meaningfully on the eval slice, we know tracker-grounding works and it's worth investing in Tier B (fine-tune YOLOv11 on SoccerNet-Tracking-2024 on the new GPU box). If it doesn't move, we've spent two days and learned that pixel-level grounding isn't the bottleneck (it's something else, like LLM interpretive bias).

## Related pieces already in the repo

- `experiments/ai_commentator/live_srt_run.py` — where the tracker output would be consumed
- `experiments/ai_commentator/rich_context.py` — the natural place to add the "TRACKING" section
- `experiments/ai_commentator/judge.py` — measurement harness stays as-is

## Open questions

- **Should the tracker output be structured JSON or natural-language text in the prompt?** Text may be easier for the LLM to consume; JSON is easier to keep consistent. Try both.
- **How much of the tracker output to include per burst?** Just the newest frame's tracking? Or all 5 frames? Likely just the newest to keep prompt size bounded.
- **Cache tracker output between snapshots for reproducibility?** For evaluation, yes — save per-frame tracker JSON alongside the video, run each pipeline against the same tracker output.
- **What to do when tracker is uncertain?** The tracker should have a confidence field per detection. The LLM should treat low-confidence detections as absent, not as facts.

# Review Cycle 1 — Disposition of Every Reviewer Comment

Reviewer: Alex · Sheet: 2026-07-20 · Clip: Mainz vs Union (dev set)
Contract: **every comment is either addressed by a GENERIC rule (with run evidence)
or rejected with a stated reason.** Rules live in `experiments/ai_commentator/tuning_rules.yaml`;
gate evidence in the committed acceptance-trio snapshots (`m1..m3.json` for the trio-9
acceptance; `r1..r3.json` for the trio-12 R11 acceptance).

| # | Time | Comment | Disposition | Rule | Evidence |
|---|---|---|---|---|---|
| 1 | 0:00 | "Very good" (opener) | ✅ retained | — | Opener protected; first line 0.8 s in every gated run |
| 2 | 0:06 | "Why no mention of the yellow card?" | ⚠️ **addressed-when-detected** | R1 | Priority preempt proven live (187 s card called once, correctly; team-agnostic 30 s dedup). The 0:06 instance was root-caused to **detector burst-skip under worker saturation** — fixed structurally (4 workers, F3); when a high-conf card detection exists, the R1 fixture machine-checks a line within 8 s. Residual: detection recall of brief events is a vision-model limit, tracked on the ReID/vision roadmap — a rule cannot conjure a detection. |
| 3 | 0:13 | "Safe comment is pointless. Eager better" | ✅ addressed | R2 + R9 | Eager is the default voice (R9 accepted); content-floor prompt + **code gate**: filler requires ≥15 s genuine silence (F2 after trio-1's own fixture caught a leak) |
| 4 | 0:56 | "Repetition, not needed" (free-kick ×2) | ✅ addressed | R3 | 25 s fact-dedup; trio runs show zero free-kick dup pairs <25 s (fixture R3 = True in all runs) |
| 5 | 1:02 | "Not sure the French translation flies" | ✅ addressed | R7 | FR rewritten as football-French localizer; fixture bans calques in every run |
| 6 | 1:06 | "Eager is more accurate than Safe" | ✅ addressed | R9 | Eager default (accepted, human A/B 4–0) |
| 7 | 1:10 | "Safe sentence means nothing" | ✅ addressed | R2 | As #3 |
| 8 | 1:19 | "Eager sentence more accurate" | ✅ addressed | R9 | As #6 |
| 9 | 1:24 | "Strange combo — previous sentence says exact opposite" | ✅ addressed | R4 | Transition-marking prompt rule + automated flip-without-transition fixture (True in all gated runs) |
| 10 | 1:52 | '"it" refers to what?' | ✅ addressed | R5 | No-pronoun-without-antecedent prompt rule; zero pronoun-led lines in gated runs (checked) |
| 11 | 1:59 | "Wrong — says long kick, actually short pass" | ⚠️ **partially addressed** | R6 | Manner-restraint rule stops the *claim* (manner words banned unless detector provides them). The underlying mis-perception is a vision-model limit — **rejected as a rule target** (statement: rules govern what we say about evidence; they cannot correct the evidence). Roadmap: detector upgrade. |
| 12 | 2:06 | '"last 3rd" never used in French' | ✅ addressed | R7 | Verified live: "les trente derniers mètres"; banned-calque fixture in every run |
| 13 | 2:23 | 'should say "sont revenus"' | ✅ addressed | R7 | Glossary entry (players back in position → "sont revenus") |
| 14 | 2:24 | "Kind of duplicate and clear in French" | ✅ addressed | R3 + R7 | Dedup window + localizer |
| 15 | 2:27 | "Not accurate sentence" | ⚠️ **partially addressed** | R6 | Same statement as #11: precision-restraint reduces wrong specifics; perception accuracy itself is out of rule scope (vision roadmap) |
| 16 | 2:31 | "Repeat sentence not needed" | ✅ addressed | R3 | As #4 |
| 17 | 2:38 | '"sonder" not applicable to football' | ✅ addressed | R7 | Glossary: sonder → tenter/essayer; fixture-banned |
| 18 | 2:48 | "aforementioned ≠ famous" | ✅ addressed | R7 | Localizer verified: "Et voici Derrick Kohn" (no invented "fameux") |
| 19 | 3:16 | '"moment calme" not appropriate' | ✅ addressed | R7 | Glossary: → "temps faible"; verified live |
| 20 | 3:18 | "much better than above" | ✅ retained | — | Positive signal on eager-style line; eager is default |
| 21 | 3:20 | 'substitution, not "change of foot" — all 4 languages inaccurate' | ✅ addressed | R8 | ASR-sanity veto **proven in-run**: `[veto] 'Meanwhile, changes of foot.' — ASR-suspect during substitution`; phrase absent from all gated outputs; pool pre-vetted at prewarm (zero latency cost after F4) |

**Tally: 16 addressed · 3 partially addressed with stated residual (2, 11, 15 — all bounded by vision perception, not rules) · 2 positive/retained · 0 unaddressed.**

## Iteration log for this cycle (the gate doing its job)

| Attempt | Outcome | What the gate caught | Fix |
|---|---|---|---|
| Gate run 1 (single) | ACCEPT — **but invalid** | Post-hoc check showed R1 never fired (defect never encoded in the gate) | → adopted **pin-the-defect** + per-rule fixtures |
| Gate run 2 (single) | crash + R1 double-fire | compare() list bug; "Kohn is booked **again**" (team-lost dedup key → implied second yellow) | harness fix; team-agnostic 30 s dedup |
| Trio 1 (worst-of-3) | **REJECT** | R2 fixture failed in 1/3 runs (filler leak); survival worst 0.854 — R8's blocking sanity call taxed the critical path | R2 code gate (silence ≥15 s); R8 pool **pre-vetted at prewarm**; R8 prompt leniency (false-positive veto of a genuine idiom); 4 vision workers |
| Trio 2 | cut short | superseded by the above fixes before completion | — |
| Trio 3 | cut short | **CRITICAL post-hoc find in trio-1 evidence: run c2 spoke "Mainz have scored!" — a FALSE GOAL** from two high-conf detections 0.55 s apart (adjacent bursts share 3/4 frames — not independent evidence). The LLM frame-judge missed it. | **R10**: goal calls require ≥3 high-conf detections spanning ≥5 s (net + celebration + aftermath); deterministic fixture added — a goal-call line without that backing fails the build |
| Trio 4–8 | cut/REJECT | in order: veto bypass + dedup hole (F5/F6) · fixture/R10 contradiction + real-second-sub false flag (fixture fixes) · unmarked flip via text/detector mismatch (F9) · filler domain mismatch (F10) · **completion-order audio race** (F11 ordered writes) | each failure became a permanent deterministic guard |
| **Trio 9** | **ACCEPT** | survival [1.0, 0.974, 0.956] · hallucinations [0,0,0] · all 9 automated fixtures green in all runs · named lines [11,11,10] vs baseline 4 | batch R1–R8 + R10 accepted; baseline advanced to the worst passing run |

## Reviewer-visible verification shortcuts (for Alex's next pass)

- 0:06-class events: any high-conf card/goal now yields a line within 8 s (or the R1 fixture fails the build)
- 0:56-class repeats: cannot recur within 25 s without new info
- French: the three banned calques cannot appear (build-failing fixture); glossary is
  reviewer-extendable in `tuning_rules.yaml`
- 3:20-class ASR errors: vetoed pre-speech; vetoes are logged in the run output

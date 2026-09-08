# Model E (vendor) commentary pipeline — architecture & operations

> The CLEAR reference for the vendor-commentary trial pipeline, 2026-09-08. Supersedes the
> running notes in [eros_vendor_test.md](eros_vendor_test.md) (kept for findings history).
> "Model E" is the mandated reviewer-facing name; the vendor's identity, API base URL and
> all tokens live ONLY in `.env` (`EROS_API_BASE`, `EROS_MATCH_TOKEN`, `EROS_STREAM_TOKEN`,
> `SLACK_WEBHOOK`). Never put any of these in code, pages, URLs, or Slack.

## What it is

A live pipeline that sends match video to an external AI commentary service and turns its
timed TEXT output into reviewable, voiced, synced videos in many languages:

```
clip/broadcast ──ffmpeg -re──SRT──▶ Model E  (vision → native zh-CN text → per-language
                                              translation behind their safety gates)
        WebSocket (per language) ◀──────────  lines: {sequence, line_id, source_pts_ms,
                                              priority 0-3, text, latency_ms, ...}
        precise voicing engine ─▶ per-language ElevenLabs track ─▶ mux over crowd bed
        ─▶ tabbed review page (text+audio+status per language) ─▶ Slack announce
```

Production shape this dummies: Sportradar SRT in → forward to Model E → text+pts back →
sync onto delayed video → record/publish into Agora.

## Contract facts (measured, not just documented)

- **Timing**: `source_pts_ms` is on OUR media timeline and is frame-accurate (verified
  against video). Their latency (frame→text): p50 ~4.3-4.6s, p95 ~5.1s; `deadline_ms`
  (we use 6000) drops anything slower — never late. WebSocket delivers ~170ms after emit.
  **7s broadcast delay** fits: text ≤6s + WS 0.2s + flash TTS ~0.3s (16 words = 0.29s).
- **Languages**: declared at match creation, immutable, translated per-line from zh-CN.
  All languages of one line share `sequence`/`line_id` and are released TOGETHER (measured:
  EN arrives ≤0.08s after zh at p90) — translation adds no delay, only rare per-language
  GAPS (~0.5%; a gap is a dropped translation, never retryable). Creation validates
  NOTHING (bogus tags get 201) — a language is supported only when it demonstrably flows.
  Verified flowing: en, fr, pt-BR, es, tr (+ native zh-CN).
- **Priority**: 0 official event · 1 corroborated shot/save/goal · 2 on-ball · 3 colour.
  LOWER NUMBER = MORE IMPORTANT. Their rule and ours: a numerically-lower line interrupts.
- **No goal announcements without an official event feed** (their safety gate; we hold no
  event token yet). Expect "celebrating wildly!" but never the word "goal".
- Streams are append-only; no revisions; sequence gaps are normal; `stream_epoch` changes
  on reconnect.

## The precise voicing engine (`eros_trial/trial.py`)

Broadcast-grade determinism, per language:
1. Every utterance is written into a raw 16kHz PCM track at `int(pts*SR)` — sample-exact
   start (≪50ms requirement).
2. If a line's moment arrives while a previous utterance is still playing:
   - **new priority < current** (more important): the running utterance is CUT with a 60ms
     fade ending exactly at the new line's start; new line begins on time. Status: `cut`.
   - otherwise the NEW line is **dropped** immediately. Status: `dropped` (+reason).
3. Nothing ever shifts later. (The old `prev+0.2` cascade produced up to +10s perceived
   lag — reviewer-caught; never reintroduce it.)
4. Full disclosure: `work_<id>/placement_<lang>.json` records every line's fate
   (`played` / `cut`+cut_at / `dropped`+reason / `tts_failed`), rendered as chips
   (✓ / ✂ / ✖) on the review page per language tab.

Delivery speeds (ElevenLabs native `voice_settings.speed`): fr 1.15 (sped-up fr reviewer-approved: "french sounds ok"), pt-BR 1.08, es 1.05, rest 1.0. Voices: dedicated EN/FR/pt-BR voices (blend-pipeline conventions); other languages use the
EN commentator voice — flash v2_5 is multilingual, pronunciation follows the text language.

## Runbook

```
cd experiments/ai_commentator/eros_trial
. /home/ubuntu/commentary/.env
# one window, live, all languages, voiced + page:
.venv python trial.py --id rN --clip <mp4> --pkg pkg_rN.json --langs en,fr,pt-BR,es,tr,zh-CN
# all five random windows + Slack announce:
./run_all_trials3.sh
# live sign-language variant (Signapse generated DURING the stream, honest lag):
.venv python live_sign_trial.py LSn <clip> <pkg>; .venv python assemble_live_sign.py LSn <clip>
```

- `match_package` (undocumented by vendor; ours works): squads with numbers/positions/
  starters, kit hex colours, formations, referee, and `kickoff_state`
  {period, clock, home_score, away_score} for mid-match windows. Build from
  `match_data/m05_uni_md33/sr_cache.json`; windows metadata in `random_windows.json`
  (clock↔file calibration for the full broadcast: 1H = clock+1797s, 2H = clock+3478s).
- Pages deploy to `/var/www/html/experiments/ai_commentator/modelE_trial<id>/`; tabs
  switch text + voiced video + status chips together; per-line comments POST to the shared
  feedback store as version `modelE<id>` with the language in `column`.
- Slack posts use the standing title convention with the `$SLACK_WEBHOOK` env var.
- Media naming: `modelE_<lang>.mp4` — the page template and the writer MUST agree (a
  mismatch once 404'd every video).

## Known limits / open items

- Full findings & ratings: [eros_vendor_test.md](eros_vendor_test.md). Headlines: zero
  fabricated facts in 226 lines; naming specificity is the weak spot (1.8% of lines name a
  player); ~39% airtime; zh-origin calques in EN.
- Vendor comms policy: do NOT tell them their latency beats the published figures — we
  want continued reduction; the feedback page omits latency entirely.
- Vendor asks outstanding: event token (turns goals/cards/subs into p0 facts),
  `match_package` schema, sentiment field (their v1 dropped it; needed for TTS emotion).
- Signapse (sign-language leg) latency swings 5→45s per clip with waves of 408/503 at a
  ~45s gateway; live signing was 14/49 lines on a bad day — needs their SLA/capacity
  answer before productising. Evidence: `eros_trial/work_LS1/live_events.json`.
- Sportradar key EXPIRED — other fixtures' match packages blocked on renewal.

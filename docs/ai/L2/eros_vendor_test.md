# Eros (nextmoment.ai) vendor commentary — evaluation setup

> **TEST RAN 2026-09-06** (vendor allowlisted our IP; they also fixed the GET-by-id 404 and
> confirmed pre-live polling). Results — 5-min clip, en+zh-CN: **35 lines per language, 0
> translation gaps; latency p50 4.34 s / p95 4.99 s** (beats published 5.1/7.0); priorities
> 3×p1, 10×p2, 22×p3 (no p0 — no event feed). Quality spot-check: opening injury scene,
> keeper actions, and the Juranovic-for-Trimmel sub (with names, from vision alone) all
> correct; missed the Kohn yellow (~188 s), left the Sieb/Weiper double sub unnamed, and did
> not call the Posch header wide (~261 s, biggest chance). No fabricated facts observed.
> Results page: sa-dev `/experiments/ai_commentator/eros_test/`.
> Updated 2026-09-05 after credentials arrived (m2.md): tokens in `.env` (EROS_MATCH_TOKEN /
> EROS_STREAM_TOKEN; no event token issued — and therefore NO goal announcements, their
> safety gate). **Live test BLOCKED: SRT ingest unreachable** (handshake unanswered; API on
> same IP fine; our UDP path verified) — full evidence + two API bugs/questions in
> `experiments/ai_commentator/eros_test/VENDOR_REPORT.md`, sent to vendor. Control-plane
> (create/arm/list/poll/end) all verified working.
> Originally added 2026-09-05, ahead of credentials. Vendor "Eros Live" implements a variant of the
> protocol we proposed in `github.com/BenWeekes/ai-commentary`; their delivered spec is
> `~/moment.md` (v2026-09-04, subtitle mode). Everything below is built and verified except
> the live run, which is blocked ONLY on tokens.

## Verdict on their API vs our proposal

Adopted (and improved) from our spec/review: PTS-anchored timestamps (`source_pts_ms` on the
source media timeline), a latency contract (claimed p50 5.1 s / p95 7 s, plus per-line
`latency_ms`), drop-late via `deadline_ms` (matches our sync philosophy), and an official
events channel (`/v1/events`, revision/retract) so goals/cards become facts.

Divergences to code around:
- **Priority 0–3, lower = MORE important** (0 = official event) — inverted vs our 1–3.
  Their interruption rule: fade the current line when a numerically lower priority arrives.
- Read model is **poll or WS pull per language** with one shared sequence space; no
  `pts_end`, no `sentiment`.
- **zh-CN is the native channel** — safety gates run on Chinese; EN is a post-gate
  translation that can fail silently (sequence gaps, do NOT retry). EN gap-rate is a key
  test metric.
- Languages are frozen at match creation; 4 separate credentials (control / stream /
  event tokens provisioned out-of-band; only the SRT publish URL is issued by `arm`).
- `match_package` schema is UNDOCUMENTED ("supplied separately") — the main open question.
- For our TTS product: their 5–7 s text + our synthesis ⇒ **~10 s broadcast delay** (their
  own doc: 6–9 s picture trail if video is not delayed). Matches our 10 s profile.

Verified empirically: API live at `live.nextmoment.ai` = 34.85.178.237 (GCP; same IP as SRT
ingest), all routes 401 without a valid bearer — no anonymous path.

## Test harness (`experiments/ai_commentator/eros_test/`)

- `run_test.py` — create match (`subtitle`, `["en","zh-CN"]`, deadline 8000; match_package
  built from `match_data/m05_uni_md33/sr_cache.json`: squads with numbers/positions/starters,
  kit hex colours, formations, referee, kickoff state 76:50 @ 1-1) → arm → `ffmpeg -re`
  publish of `clips/m05_uni_eval_25min/slice_5min.mp4` (720p, within their guidance) →
  poll both languages, save `subs_en.jsonl` / `subs_zh_CN.jsonl` + latency stats.
  Run: `EROS_CONTROL_TOKEN=… EROS_STREAM_TOKEN=… python3 run_test.py`
- `post_events.py` — posts the window's two real official moments live (Kohn yellow
  ~188.1 s; Sieb+Weiper double sub ~202.4 s) to test their events-improve-accuracy claim.
  Needs `EROS_EVENT_TOKEN` + `EROS_MATCH_ID`; run alongside the publish.
- `build_page.py` — results page at
  `https://sa-dev.agora.io/experiments/ai_commentator/eros_test/`: click-to-seek clip +
  four aligned columns (human broadcaster STT · our v7 AI · Eros EN with priority/latency ·
  Eros zh-CN), header stats (line counts, EN translation-gap count, measured latency
  percentiles vs claimed, priority distribution). `--mock` renders a layout preview
  (currently deployed with a MOCK banner until the live run).

## Naming: "Model E" in ALL reviewer-facing surfaces

Per user instruction (2026-09-06): review pages, Slack posts and anything shareable say
**Model E**, never the vendor's name. Vendor identity + API base live in `.env`
(`EROS_API_BASE`); repo code and pages carry no vendor branding. (This file keeps the
`eros_` path names — they predate the rule and URLs were already shared.)

## Round 2 findings (2026-09-06): WebSocket, 7 s budget, five random windows

- **WebSocket works**: lines arrive median **168 ms** after emit (vs ~1 s polling);
  `trial.py` now reads WS with auto-reconnect from last sequence. `deadline_ms` default
  lowered to **6000** → full chain (text ≤6 s + WS ~0.2 s + flash TTS ~0.3 s, measured)
  fits a **7 s broadcast delay** with slack; measured TTS: 16-word line synthesises in
  0.29 s.
- **Full Mainz–Union broadcast** now on disk (`clips/md33_full/`, 2h35m; other two MD33
  games + zip deleted to reclaim 16 G). Clock↔file calibration: 1H = clock+1797 s,
  2H = clock+3478 s. Goals 37' 48' 88' 90'. Sportradar key EXPIRED (renewal needed for
  other fixtures); OpenLigaDB (free) provides fixtures/goals for all of MD33.
- **Trials r1–r5** (random in-play windows, `make_random_clips.py` + `run_all_trials.sh`):
  35–43 lines per window, latency p50 4.2–4.6 s, 1 translation gap in 191 lines (a p3
  filler). Goal window (r2, Ilic 37'): build-up and "Union Berlin are celebrating wildly!"
  but no goal call — gate works; one error ("goalkeeper catches") on the goal moment.
- **Rating**: ~6.5/10 as commentary, ~8/10 as the safe subtitle product it claims to be.
  Weakest: naming specificity, coverage (~39% airtime), zh-origin phrasing
  ("frontcourt"). Strongest: grounding (zero fabrications in 226 lines), timing honesty.
  Next lever: their event token (turns missed cards/goals/subs into p0 facts) + a
  football-glossary pass like our R7 loop.

## Trial pipeline (repeatable per clip) — `experiments/ai_commentator/eros_trial/`

The production shape (Sportradar SRT → forward to Eros → text+pts → sync onto the clip →
record → publish) is dummied end-to-end for review:

- `trial.py --id N --clip <mp4> --pkg <match_package.json>` — creates/arms an Eros match,
  publishes the clip in real time, captures en+zh-CN, voices the EN lines with our
  ElevenLabs commentator at `source_pts_ms` (overlap lines shift later, never earlier),
  muxes over the crowd bed (`mux_with_crowd.py`), and builds the review page. Work dirs
  `eros_trial/work_<N>/` cache everything (`--skip-eros` rebuilds page/voice only).
- `build_trial_page.py` — review page in the house style: voiced video, collapsible
  **pre-match data actually sent**, line table (click-seek, auto-follow, priority/latency
  chips, overlap-shift notes), per-line comments POSTing to the shared feedback store as
  version `eros<id>` (unknown versions store normally; only closed rounds are late-archived).
- Trial 1 (5-min Mainz–Union): `https://sa-dev.agora.io/experiments/ai_commentator/eros_trial1/`
  — announced on Slack per the standing title convention.
- New clips need: the mp4 + a match_package json (build from Sportradar lineups as in
  `eros_test/run_test.py`); full-match media pending (MD33 zip upload in progress).

## What we asked the vendor for

Control + stream tokens (event token optional), the `match_package` schema, environment/
billing status, ingest IP allowlisting (our egress 3.9.234.40), expected EN gap-rate.

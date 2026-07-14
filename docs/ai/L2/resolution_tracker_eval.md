# 1080p vs 720p — tracker & vision player-identity evaluation

**Date:** 2026-07-14
**Clip:** Dortmund vs Eintracht (`~/de_bl_dortmund_eintracht.mp4`, 1920×1080, 50 fps,
123 min), 5-minute extract **20:00–25:00**. Chosen past the warm-up, different game
from the Mainz/Union work so nothing is memorised.
**Question:** does 1080p produce materially better player identity than 720p, for
(a) the YOLO+OCR tracker and (b) the gpt-5.6 vision detector? And should OCR read
the **shorts** number as well as the shirt?

## TL;DR

- **1080p clearly helps the tracker/easyOCR path (+22–28%)**; it barely helps the
  vision LLM (+7% count, but more confident reads). Vision is largely
  resolution-robust; easyOCR is resolution-hungry.
- **Reading the shorts number was worth adding** — ~10% of tracker reads come from
  shorts, and 1080p lifts shorts reads most (+76%).
- **Frame rate matters more than resolution for identity.** Going 5→10 fps *doubled*
  tracker identity coverage (9.6%→19.1%), because denser frames stop BoT-SORT
  fragmenting.
- **The bottleneck is tracking robustness, not pixels.** BoT-SORT fragments into
  ~600 tracks for ~22 players because **ReID is off** and the broadcast is full of
  camera cuts / replays / close-ups that motion-only association can't survive.

## Method

Two clean slices produced with ffmpeg (`res_test/dortmund_{1080p,720p}.mp4`),
720p is a downscale of the same re-encoded 1080p slice so content is identical.

**Tracker path — the real upgrade (`run_tracker_tracked.py`).** Previously the
"tracker" was *stateless per-frame detection* (no IDs). This eval replaces that with
**BoT-SORT multi-object tracking** (`model.track(persist=True, tracker='botsort.yaml')`,
ultralytics 8.4.89) so each player holds a persistent id, then:
1. OCR a track **opportunistically** — only while still unnamed, only on boxes ≥34 px
   tall, at most every 3rd appearance (the number does NOT need to be read every frame);
2. **vote** the number over the track's life (sum of OCR confidence per digit);
3. **propagate** the winning identity to *every* frame of that track.

Headline metric becomes **% of tracks we can name** and **frame identity coverage**
(share of player-detections that inherit a name once propagated) — not raw per-frame
read-rate.

**OCR both regions (`run_tracker_detector.py::read_number`).** Now crops **shirt**
(`0.12–0.52 h`) *and* **shorts** (`0.55–0.82 h`), 3× upscales each, easyOCR digits,
keeps the higher-confidence read, records which region won.

**Vision path (`run_events_detector.py --model gpt-5.6`).** First run used the existing
`events_detector_v1.txt`, which hard-codes "home = Mainz red / away = Union olive" and
only reads a number when kit colour matches — so on Dortmund/Eintracht it read **0**
numbers at both resolutions (a prompt artefact, not a resolution result). Re-ran with
a new team-agnostic **`events_detector_generic.txt`** that reads any legible shirt/shorts
number regardless of team. 68 bursts per resolution (stride 8).

## Results

### Tracker — BoT-SORT + OCR-vote-and-propagate

| Metric | 720p @5fps | 1080p @5fps | 1080p @10fps |
|---|---|---|---|
| OCR reads — shirt | 178 | 220 | 329 |
| OCR reads — shorts | 17 | 30 | 54 |
| OCR reads — total | 195 | 250 | 383 |
| Tracks persisted (≥5 frames) | 624 | 628 | 596 |
| **Tracks named** | 38 | 47 | 78 |
| Track name-rate | 6.1% | 7.5% | 13.1% |
| **Frame identity coverage** | 7.9% | 9.6% | **19.1%** |

- **1080p @5fps vs 720p @5fps:** +28% reads, +24% tracks named, +22% coverage. Real,
  consistent — bigger numbers = more pixels for easyOCR.
- **Shorts** contribute ~9–14% of reads and scale hardest with resolution (17→30, +76%),
  because shorts digits are tiny and pixel-starved at 720p.
- **10 fps doubles coverage** (9.6%→19.1%) at the same resolution: denser frames give
  BoT-SORT better association, so tracks live longer and each read propagates further.

### Vision — gpt-5.6, generic number-reading prompt (68 bursts)

| Metric | 720p | 1080p |
|---|---|---|
| Bursts where a number was read | 28 (41%) | 30 (44%) |
| — from shirt / shorts | 22 / 6 | 24 / 6 |
| Possession confidence high / med / low | 37 / 20 / 11 | 41 / 16 / 11 |
| Latency per burst (indicative) | ~4.5 s | ~5.2 s (+15%) |

- **1080p barely helps the vision LLM** (+2 reads, +7%) — but shifts confidence upward
  (high 37→41). The model already reads the ball-carrier's number ~41% of the time at
  720p; extra pixels add little (the API tiles/downscales large images, and the model
  leans on context), while costing ~15% more latency and more tokens.
- **The vision LLM is a better ball-carrier number reader than easyOCR** and is
  resolution-robust. Strategic implication: use the **vision LLM to read the carrier's
  number** and the **tracker to propagate** that identity across the track — better than
  easyOCR for the point read, better than vision alone for continuity.

## Honest limitations

- **BoT-SORT ReID is off** (`with_reid: False`); GMC (camera-motion comp) is on but not
  enough. ~600 tracks for ~22 players = heavy fragmentation from cuts/replays/close-ups.
- The tracker's `kit_team` is hard-coded to Mainz-red/Union-olive, so **team labels are
  meaningless on this clip** — but OCR read-rate / identity coverage (what we measured)
  are team-agnostic and valid.
- Identity coverage is per *player-frame*; even 19% means most frames still unnamed. The
  ceiling here is tracking, not OCR.

## Recommended next steps (biggest lever first)

1. **Enable BoT-SORT ReID** (`with_reid: True`, appearance embeddings) + tune
   `track_buffer` — re-identify players after occlusions and short cuts.
2. **Shot-boundary / replay detection** — reset or gate tracking across hard cuts so IDs
   don't shatter (this is a big share of the 600 tracks).
3. **Track at 10 fps+**, not 2–5 — cheap and already worth ~2× coverage.
4. **Hybrid identity:** vision LLM reads the carrier number (resolution-robust, ~41%),
   tracker propagates it — instead of relying on easyOCR for the point read.
5. This is the **SoccerNet Game-State-Reconstruction** problem; its stack (tracking +
   ReID + pitch localisation + jersey ID) is the reference target.

## Artefacts (`experiments/ai_commentator/`)

- `run_tracker_tracked.py` — BoT-SORT + OCR vote/propagate (new)
- `run_tracker_detector.py` — `read_number` now OCRs shirt **and** shorts
- `run_events_detector.py` — added `--frames-dir`, `--prompt`; `prompts/events_detector_generic.txt`
- `ocr_res_test.py` — naive per-player OCR read-rate helper
- `res_test/` — slices + `tracked_{720,1080,1080_dense}.json`, `gen56_{720,1080}.jsonl`
  (the `.mp4`s and `frames_*` are large and regenerable from the source; safe to delete)

Re-run:
```bash
cd /home/ubuntu/commentary/experiments/ai_commentator
ffmpeg -ss 1200 -i ~/de_bl_dortmund_eintracht.mp4 -t 300 -c:v libx264 -crf 18 -an res_test/dortmund_1080p.mp4
ffmpeg -i res_test/dortmund_1080p.mp4 -vf scale=1280:720 -c:v libx264 -crf 18 -an res_test/dortmund_720p.mp4
.venv-track/bin/python run_tracker_tracked.py res_test/dortmund_1080p.mp4 5 --json res_test/tracked_1080_dense.json
.venv/bin/python run_events_detector.py --model gpt-5.6 --stride 8 \
  --prompt prompts/events_detector_generic.txt --frames-dir res_test/frames_1080 --out res_test/gen56_1080.jsonl
```

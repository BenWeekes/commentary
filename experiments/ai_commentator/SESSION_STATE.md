# Session state — reviewer feedback UX + per-clip corpus

## What just changed (2026-07-22)
Reviewer-facing feedback fixes on the blend results pages + durability guarantees.

### Page (`build_hybrid_page.py`)
- **Auto-scroll no longer fights reviewers.** Auto-follow of the playing row is suspended
  while any comment box is open (`window.__fbFrozen`) and for 6 s after any manual wheel/touch
  scroll (`window.__fbPauseUntil`). A cell click also `stopPropagation()`s so it never seeks
  the video.
- **Pending-review icon + edit.** A commented-but-unsent cell shows 📝; click again to edit or
  Remove; after Submit it turns green ✓. Comments are held in `pendingByCell` (Map) and are
  fully editable until sent.
- **Every comment is now keyed** by `clip` (CLIP_ID, default `mainz_union_md33_76-81`) and
  `profile` (`6s`/`10s`, derived from PAGE_VERSION/artifact suffix) in addition to
  `version, row-time, col, column`. This is what makes 6s-vs-10s feedback distinguishable —
  previously both pages posted only `version:"v4"`.

### Backend (`submit_server.py`)
- `_clean()` now keeps `column`, `profile`, `clip` (it was silently dropping `column` before).
- Running as systemd `blend-feedback.service`; restarted to pick up the change.

### Corpus (`clips.yaml`, new)
- Registry of reviewed clips. Policy: every reviewed clip → permanent fixtures; the gate runs
  ALL clips every build. Feedback jsonl is append-only + git-committed, never truncated.

## ⚠️ Data-loss incident (fixed by policy)
The reviewer's first real v4 submission (reviewer "ben", "this is Ben", STT cell ~62 s,
10s page) was **destroyed** when I `>`-truncated `feedback/v4/comments.jsonl` during cleanup.
New rule (also in the L2 doc): **never truncate feedback** — filter single lines if needed.

## Live state
- Pages: `/blend_v4_10s/` and `/blend_v4_6s/` — both 200, new JS baked in, CLIP + PROFILE
  verified present.
- Round v4 is OPEN. `feedback/v4/comments.jsonl` is currently empty (awaiting real reviews).

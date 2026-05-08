"""Scheduler: one daemon thread that manages live match lifecycle.

Owns per-match state, refresh cadence, countdown tracking, and
auto-start/stop decisions.  Demo matches are tracked but never
auto-managed.

The scheduler never directly touches media pipelines — it calls
orchestrator.start_match() / stop_match() which are idempotent.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from server.config import MatchConfig, ServerConfig


# ── Per-match state ──────────────────────────────────────────────────

@dataclass
class MatchSchedule:
    """Scheduler-side state for one match."""
    match_id: str
    match_cfg: MatchConfig
    state: str = "idle"          # demo/upcoming/countdown/armed/starting/running/stopping/finished/stopped/error/waiting_for_source
    kickoff_utc: str = ""        # ISO timestamp from config or SR
    kickoff_ts: float | None = None  # parsed epoch seconds
    last_check_at: float = 0
    last_refresh_at: float = 0
    last_error: str = ""
    check_interval: float = 60.0
    refresh_interval: float = 300.0
    sr_match_status: str = ""    # SR match status: not_started/live/closed/ended
    start_error_count: int = 0   # consecutive auto-start failures


def _parse_kickoff(iso: str) -> float | None:
    """Parse an ISO 8601 kickoff string to epoch seconds. Returns None on failure."""
    if not iso:
        return None
    try:
        # Handle both "2026-05-08T18:30:00+00:00" and "2026-05-08T18:30:00Z"
        iso = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.timestamp()
    except Exception:
        return None


# ── Refresh cadence rules ────────────────────────────────────────────

def _compute_intervals(ttk: float | None) -> tuple[float, float]:
    """Return (check_interval, refresh_interval) based on time-to-kickoff.

    ttk: seconds until kickoff, or None if unknown.
    """
    if ttk is None:
        return (60.0, 300.0)       # unknown kickoff: check 60s, refresh 5min
    if ttk > 30 * 60:
        return (60.0, 600.0)       # >30min: check 60s, refresh only if stale
    if ttk > 10 * 60:
        return (60.0, 300.0)       # 30-10min: check 60s, refresh 5min
    if ttk > 3 * 60:
        return (30.0, 60.0)        # 10-3min: check 30s, refresh 60s
    return (5.0, 15.0)             # <3min: check 5s, refresh 15s


# ── Scheduler ────────────────────────────────────────────────────────

class Scheduler:
    """Single daemon thread that manages live match lifecycle."""

    def __init__(self, config: ServerConfig, orchestrator, match_store):
        self._config = config
        self._orchestrator = orchestrator
        self._match_store = match_store
        self._schedules: dict[str, MatchSchedule] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        for mc in config.matches:
            ms = MatchSchedule(
                match_id=mc.match_id,
                match_cfg=mc,
            )
            # Seed kickoff from config
            if mc.kickoff_utc:
                ms.kickoff_utc = mc.kickoff_utc
                ms.kickoff_ts = _parse_kickoff(mc.kickoff_utc)

            # Also check match_store for a refreshed kickoff
            meta = match_store.read_match_meta(mc.match_id)
            if meta and meta.get("kickoff_utc"):
                ms.kickoff_utc = meta["kickoff_utc"]
                parsed = _parse_kickoff(meta["kickoff_utc"])
                if parsed:
                    ms.kickoff_ts = parsed
            if meta and meta.get("last_refresh_at"):
                ms.last_refresh_at = meta["last_refresh_at"]

            # Set initial state
            if mc.mode == "demo":
                ms.state = "demo"
            elif not mc.enabled:
                ms.state = "stopped"
            elif not mc.auto_manage:
                ms.state = "upcoming"
            else:
                ms.state = "upcoming"

            self._schedules[mc.match_id] = ms

    def start(self):
        """Start the scheduler daemon thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[SCHED] Scheduler started")

    def stop(self):
        """Signal the scheduler to stop."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        print("[SCHED] Scheduler stopped")

    def get_schedule(self, match_id: str) -> MatchSchedule | None:
        return self._schedules.get(match_id)

    def get_all_schedules(self) -> dict[str, MatchSchedule]:
        return dict(self._schedules)

    # ── Main loop ────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            now = time.time()
            min_sleep = 5.0  # minimum loop interval

            for mid, ms in self._schedules.items():
                if self._stop.is_set():
                    break

                # Skip non-auto-managed matches
                if ms.state == "demo" or not ms.match_cfg.auto_manage:
                    self._sync_worker_state(ms)
                    continue
                if not ms.match_cfg.enabled:
                    ms.state = "stopped"
                    continue

                # Check if it's time to check this match
                if now - ms.last_check_at < ms.check_interval:
                    remaining = ms.check_interval - (now - ms.last_check_at)
                    min_sleep = min(min_sleep, remaining)
                    continue

                ms.last_check_at = now

                try:
                    self._tick_match(ms, now)
                except Exception as e:
                    ms.last_error = str(e)
                    print(f"[SCHED] {mid} error: {e}")

            # Sleep until next check needed
            self._stop.wait(timeout=max(1.0, min_sleep))

    _MAX_START_ERRORS = 3  # max consecutive auto-start failures before giving up

    def _tick_match(self, ms: MatchSchedule, now: float):
        """One scheduler tick for a live auto-managed match."""
        mid = ms.match_id

        # Compute time-to-kickoff
        ttk = None
        if ms.kickoff_ts:
            ttk = ms.kickoff_ts - now

        # Update intervals based on proximity to kickoff
        ms.check_interval, ms.refresh_interval = _compute_intervals(ttk)

        # Check if we need to refresh SR data
        needs_refresh = False
        if ms.last_refresh_at == 0:
            needs_refresh = True  # never refreshed
        elif now - ms.last_refresh_at > ms.refresh_interval:
            needs_refresh = True

        if needs_refresh:
            self._do_refresh(ms, now)

        # Get current worker state
        worker = self._orchestrator.get_worker(mid)
        worker_state = worker.status.state if worker else "idle"

        # State machine transitions
        if worker_state in ("starting", "running"):
            ms.start_error_count = 0  # reset on successful start

            # If already stopping, don't overwrite state or spawn another thread
            if ms.state == "stopping":
                return

            ms.state = "running" if worker_state == "running" else "starting"

            # Auto-stop: if SR reports match closed/ended, stop asynchronously
            if worker_state == "running" and ms.sr_match_status in ("closed", "ended"):
                ms.state = "stopping"
                print(f"[SCHED] {mid} SR reports '{ms.sr_match_status}' — stopping async")
                threading.Thread(
                    target=self._async_stop, args=(ms,), daemon=True
                ).start()
            return

        if worker_state == "stopped" and ms.state == "running":
            # Worker stopped (demo completed or was stopped) — mark finished
            ms.state = "finished"
            print(f"[SCHED] {mid} finished (worker stopped)")
            return

        # Worker errored — count failures and stop retrying after max
        if worker_state == "error" and ms.state in ("starting", "running"):
            ms.start_error_count += 1
            if ms.start_error_count >= self._MAX_START_ERRORS:
                ms.state = "error"
                ms.last_error = f"worker failed {ms.start_error_count} times"
                print(f"[SCHED] {mid} giving up after {ms.start_error_count} errors")
                return
            else:
                print(f"[SCHED] {mid} worker error ({ms.start_error_count}/{self._MAX_START_ERRORS})")
                # Fall through to re-evaluate kickoff logic

        if ms.state in ("finished", "error"):
            # Terminal states — don't auto-restart
            return

        # Worker is idle — decide what to do based on kickoff proximity
        if ttk is None:
            # No kickoff known — stay upcoming, wait for refresh to discover it
            ms.state = "upcoming"
            return

        prestart_seconds = max(0.0, ms.match_cfg.prestart_seconds)

        if ttk > prestart_seconds:
            ms.state = "upcoming"
            return

        has_keyterms = self._match_store.read_keyterms(mid) is not None
        ms.state = "starting"
        keyterms_note = "" if has_keyterms else " without keyterms"
        print(f"[SCHED] {mid} auto-starting (kickoff in {ttk:.0f}s, prestart={prestart_seconds:.0f}s){keyterms_note}")
        try:
            self._orchestrator.start_match(mid)
        except Exception as e:
            ms.start_error_count += 1
            if ms.start_error_count >= self._MAX_START_ERRORS:
                ms.state = "error"
                ms.last_error = f"auto-start failed {ms.start_error_count} times: {e}"
                print(f"[SCHED] {mid} giving up after {ms.start_error_count} errors")
            else:
                ms.state = "upcoming"
                ms.last_error = str(e)
                print(f"[SCHED] {mid} auto-start failed ({ms.start_error_count}/{self._MAX_START_ERRORS}): {e}")

    def _do_refresh(self, ms: MatchSchedule, now: float):
        """Attempt to refresh SR data for a match."""
        mid = ms.match_id
        api_key = self._config.sportradar_api_key
        if not api_key:
            return

        from server.sr_data import refresh_match_data
        result = refresh_match_data(mid, ms.match_cfg, self._match_store, api_key)

        if result.get("status") == "ok":
            ms.last_refresh_at = now
            # Update kickoff if SR provided one
            if result.get("kickoff_utc"):
                ms.kickoff_utc = result["kickoff_utc"]
                parsed = _parse_kickoff(result["kickoff_utc"])
                if parsed:
                    ms.kickoff_ts = parsed
            # Track SR match status for auto-stop
            if result.get("match_status"):
                ms.sr_match_status = result["match_status"]
            print(f"[SCHED] {mid} refreshed: {result.get('keyterm_count', 0)} keyterms, "
                  f"kickoff={ms.kickoff_utc or 'unknown'}, sr_status={ms.sr_match_status or '-'}")
        elif result.get("status") == "already_refreshing":
            pass  # another thread is handling it
        else:
            ms.last_error = result.get("error", result.get("status", "unknown"))
            print(f"[SCHED] {mid} refresh failed: {ms.last_error}")

    def _async_stop(self, ms: MatchSchedule):
        """Stop a match in a background thread so the scheduler loop isn't blocked."""
        mid = ms.match_id
        try:
            self._orchestrator.stop_match(mid)
            ms.state = "finished"
            print(f"[SCHED] {mid} stopped successfully")
        except Exception as e:
            ms.last_error = str(e)
            ms.state = "error"
            print(f"[SCHED] {mid} async stop failed: {e}")

    def _sync_worker_state(self, ms: MatchSchedule):
        """For non-auto-managed matches, sync state from worker."""
        worker = self._orchestrator.get_worker(ms.match_id)
        if not worker:
            return
        ws = worker.status.state
        if ws in ("starting", "running"):
            ms.state = "running" if ws == "running" else "starting"
        elif ws == "stopped" and ms.state in ("running", "starting"):
            ms.state = "stopped"

"""Orchestrator: manages multiple MatchWorkers."""

import threading

from server.config import ServerConfig
from server.match_store import MatchStore
from server.match_worker import MatchWorker
from server.scheduler import Scheduler


class Orchestrator:
    """Start, stop, and query multiple match workers."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._workers: dict[str, MatchWorker] = {}
        self._match_locks: dict[str, threading.Lock] = {}
        self._match_store = MatchStore()

        for match_cfg in config.matches:
            self._match_store.ensure_match_dir(match_cfg.match_id)
            self._workers[match_cfg.match_id] = MatchWorker(
                match_cfg, config, match_store=self._match_store)
            self._match_locks[match_cfg.match_id] = threading.Lock()

        self._scheduler = Scheduler(config, self, self._match_store)

    @property
    def match_store(self) -> MatchStore:
        return self._match_store

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    def start_all(self):
        """Start a MatchWorker per configured match."""
        for mid, worker in self._workers.items():
            print(f"[ORCH] Starting match: {mid}")
            worker.start()

    def stop_all(self):
        """Stop all workers."""
        for mid, worker in self._workers.items():
            print(f"[ORCH] Stopping match: {mid}")
            worker.stop()

    def start_match(self, match_id: str, stt_provider: str | None = None,
                    stt_endpoint_delay_ms: int | None = None,
                    pipeline_mode: str | None = None,
                    speech_translation_provider: str | None = None):
        """Start a single match worker. Noop if already running.
        Per-match lock prevents double-start without blocking other matches."""
        worker = self._workers.get(match_id)
        if not worker:
            raise KeyError(f"match '{match_id}' not found")
        with self._match_locks[match_id]:
            if worker.status.state in ("starting", "running"):
                return
            worker.configure_runtime(
                stt_provider=stt_provider,
                stt_endpoint_delay_ms=stt_endpoint_delay_ms,
                pipeline_mode=pipeline_mode,
                speech_translation_provider=speech_translation_provider,
            )
            print(f"[ORCH] Starting match: {match_id}")
            worker.start()

    def stop_match(self, match_id: str):
        """Stop a single match worker. Noop if not running.
        Per-match lock prevents races with concurrent start/stop."""
        worker = self._workers.get(match_id)
        if not worker:
            raise KeyError(f"match '{match_id}' not found")
        with self._match_locks[match_id]:
            if worker.status.state not in ("starting", "running"):
                return
            print(f"[ORCH] Stopping match: {match_id}")
            worker.stop()

    def get_all_status(self) -> dict:
        """Return {match_id: MatchStatus} for all matches."""
        return {mid: worker.status for mid, worker in self._workers.items()}

    def get_worker(self, match_id: str) -> MatchWorker | None:
        return self._workers.get(match_id)

    @property
    def match_ids(self) -> list[str]:
        return list(self._workers.keys())

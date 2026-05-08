"""Orchestrator: manages multiple MatchWorkers."""

from server.config import ServerConfig
from server.match_worker import MatchWorker


class Orchestrator:
    """Start, stop, and query multiple match workers."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._workers: dict[str, MatchWorker] = {}

        for match_cfg in config.matches:
            self._workers[match_cfg.match_id] = MatchWorker(match_cfg, config)

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

    def start_match(self, match_id: str):
        """Start a single match worker. Noop if already running."""
        worker = self._workers.get(match_id)
        if not worker:
            raise KeyError(f"match '{match_id}' not found")
        if worker.status.state in ("starting", "running"):
            return
        print(f"[ORCH] Starting match: {match_id}")
        worker.start()

    def stop_match(self, match_id: str):
        """Stop a single match worker. Noop if not running."""
        worker = self._workers.get(match_id)
        if not worker:
            raise KeyError(f"match '{match_id}' not found")
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

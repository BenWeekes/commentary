"""Disk-backed per-match data store.

Layout:
    match_data/{match_id}/
        keyterms.txt          # one term per line (human-editable)
        roster.json           # player roster for translation prompts
        match.json            # match metadata (last_refresh_at, etc.)
        sr_cache.json         # raw Sportradar API response cache
        runs/
            {YYYYMMDD_HHMMSS}/
                stt.jsonl
                {lang}.jsonl
            ...
        latest_run.txt        # symlink-like pointer: contains run dir name

All writes are atomic: write to .tmp, then os.rename() into place.
"""

import json
import os
import time


class MatchStore:
    """Read/write per-match data under a base directory."""

    def __init__(self, base_dir: str = "match_data"):
        self._base = os.path.abspath(base_dir)

    def _match_dir(self, match_id: str) -> str:
        return os.path.join(self._base, match_id)

    def ensure_match_dir(self, match_id: str) -> str:
        """Create match_data/{match_id}/ if missing. Returns the path."""
        d = self._match_dir(match_id)
        os.makedirs(d, exist_ok=True)
        return d

    # ── Atomic JSON read/write ──────────────────────────────────────────

    def _read_json(self, match_id: str, filename: str):
        path = os.path.join(self._match_dir(match_id), filename)
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            return json.load(f)

    def _write_json(self, match_id: str, filename: str, data):
        d = self.ensure_match_dir(match_id)
        path = os.path.join(d, filename)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.rename(tmp, path)

    # ── Keyterms ────────────────────────────────────────────────────────

    def read_keyterms(self, match_id: str) -> list[str] | None:
        """Read keyterms.txt — one term per line, # comments skipped.

        Returns list of terms, or None if file does not exist.
        """
        path = os.path.join(self._match_dir(match_id), "keyterms.txt")
        if not os.path.isfile(path):
            return None
        terms = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
        return terms if terms else None

    def write_keyterms(self, match_id: str, terms: list[str]):
        """Write keyterms.txt atomically."""
        d = self.ensure_match_dir(match_id)
        path = os.path.join(d, "keyterms.txt")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            for term in terms:
                f.write(term + "\n")
        os.rename(tmp, path)

    # ── Roster ──────────────────────────────────────────────────────────

    def read_roster(self, match_id: str) -> dict | None:
        return self._read_json(match_id, "roster.json")

    def write_roster(self, match_id: str, data: dict):
        self._write_json(match_id, "roster.json", data)

    # ── Match metadata ──────────────────────────────────────────────────

    def read_match_meta(self, match_id: str) -> dict | None:
        return self._read_json(match_id, "match.json")

    def write_match_meta(self, match_id: str, data: dict):
        self._write_json(match_id, "match.json", data)

    # ── SR cache ────────────────────────────────────────────────────────

    def read_sr_cache(self, match_id: str) -> dict | None:
        return self._read_json(match_id, "sr_cache.json")

    def write_sr_cache(self, match_id: str, data: dict):
        self._write_json(match_id, "sr_cache.json", data)

    # ── Run directories ─────────────────────────────────────────────────

    def get_run_dir(self, match_id: str) -> str:
        """Create match_data/{match_id}/runs/{YYYYMMDD_HHMMSS}/ and update latest_run.txt.

        Returns the absolute path to the new run directory.
        """
        ts = time.strftime("%Y%m%d_%H%M%S")
        runs_dir = os.path.join(self.ensure_match_dir(match_id), "runs")
        os.makedirs(runs_dir, exist_ok=True)
        run_dir = os.path.join(runs_dir, ts)
        os.makedirs(run_dir, exist_ok=True)

        # Write latest_run.txt pointer
        latest_path = os.path.join(self._match_dir(match_id), "latest_run.txt")
        tmp = latest_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(ts + "\n")
        os.rename(tmp, latest_path)

        return run_dir

    def list_runs(self, match_id: str) -> list[str]:
        """List run timestamps, sorted descending (newest first)."""
        runs_dir = os.path.join(self._match_dir(match_id), "runs")
        if not os.path.isdir(runs_dir):
            return []
        entries = []
        for name in os.listdir(runs_dir):
            if os.path.isdir(os.path.join(runs_dir, name)):
                entries.append(name)
        entries.sort(reverse=True)
        return entries

    def get_latest_run_dir(self, match_id: str) -> str | None:
        """Return absolute path to the latest run directory, or None."""
        runs = self.list_runs(match_id)
        if not runs:
            return None
        return os.path.join(self._match_dir(match_id), "runs", runs[0])

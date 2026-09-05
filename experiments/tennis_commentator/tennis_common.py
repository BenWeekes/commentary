#!/usr/bin/env python3
"""Shared, tennis-only configuration and safety helpers."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterable

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
CONFIG = json.loads((BASE / "config.json").read_text())
VERSION = CONFIG["version"]
INPUT_VERSION = CONFIG.get("input_artifacts_version", VERSION)
PROFILES = tuple(
    f"{float(value):g}s"
    for value in CONFIG["timing"]["fixed_delay_profiles_seconds"]
)
PROFILE = os.environ.get("TENNIS_PROFILE", PROFILES[0])
if PROFILE not in PROFILES:
    raise RuntimeError(
        f"unsupported TENNIS_PROFILE={PROFILE!r}; expected one of {PROFILES}"
    )
DELAY_S = float(PROFILE.removesuffix("s"))
SHARED_ARTIFACTS = BASE / "artifacts" / INPUT_VERSION
OUTPUT_ARTIFACTS = BASE / "artifacts" / VERSION
ARTIFACTS = OUTPUT_ARTIFACTS / PROFILE
CLIP = SHARED_ARTIFACTS / "clip.mp4"


class PipelineError(RuntimeError):
    pass


def load_env() -> None:
    """Load the repo .env without overwriting explicitly supplied values."""
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(names: Iterable[str]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise PipelineError("missing required environment variables: " + ", ".join(missing))


def football_processes() -> list[str]:
    """Return actual football workers, not shell watchers containing those names."""
    proc = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        text=True,
        capture_output=True,
        check=True,
    )
    active = []
    football_scripts = ("run_blend_live.py", "run_blend_true_live.py", "run_events_detector.py")
    for line in proc.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        _pid, command, args = fields
        is_python_worker = command.startswith("python") and any(
            re_search_token(args, script) for script in football_scripts
        )
        is_frame_receiver = command == "ffmpeg" and "/tmp/live_frames_blend/" in args
        is_football_build = (
            command.startswith("build_v") and command.endswith(".sh")
        ) or (
            command in {"bash", "sh"}
            and "/experiments/ai_commentator/build_v" in args
        )
        if is_python_worker or is_frame_receiver or is_football_build:
            active.append(line.strip())
    return active


def re_search_token(command_line: str, token: str) -> bool:
    """Match a real argv-looking token without matching a shell's quoted source text."""
    import re
    return bool(re.search(rf"(?:^|\s|/){re.escape(token)}(?:\s|$)", command_line))


def assert_football_idle() -> None:
    active = football_processes()
    if active:
        raise PipelineError(
            "football commentary workload is active; tennis build stopped to avoid interference:\n"
            + "\n".join(active)
        )


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PipelineError(f"malformed JSONL at {path}:{number}") from exc
    return rows

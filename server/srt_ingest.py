"""Helpers for bridging a remote SRT input into an internal Agora channel."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
GO_DIR = ROOT_DIR / "go-audio-video-publisher"
DEFAULT_SDK_PATH = (
    ROOT_DIR.parent / "codex" / "server-custom-llm" / "go-audio-subscriber" / "sdk" / "agora_sdk_mac"
)


def start_srt_ingest(
    *,
    srt_url: str,
    channel: str,
    publish_uid: int,
    retry_seconds: float,
    app_id: str,
    app_cert: str,
) -> subprocess.Popen:
    """Start the long-running SRT ingest publisher process."""
    env = os.environ.copy()
    env.setdefault("AGORA_APP_ID", app_id)
    env.setdefault("AGORA_APP_CERT", app_cert)
    env.setdefault("AGORA_APP_CERTIFICATE", app_cert)
    if "DYLD_LIBRARY_PATH" not in env and DEFAULT_SDK_PATH.exists():
        env["DYLD_LIBRARY_PATH"] = str(DEFAULT_SDK_PATH.resolve())

    cmd = [
        "python3", str(ROOT_DIR / "publish_srt_to_agora.py"),
        "--srt-url", srt_url,
        "--channel", channel,
        "--uid", str(publish_uid),
        "--video-mode", "yuv",
        "--retry-seconds", str(retry_seconds),
        "--max-attempts", "0",
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )


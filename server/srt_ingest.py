"""Helpers for SRT-backed publishing processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
GO_DIR = ROOT_DIR / "go-audio-video-publisher"
DEFAULT_SDK_PATH = (
    ROOT_DIR.parent / "codex" / "server-custom-llm" / "go-audio-subscriber" / "sdk" / "agora_sdk_mac"
)
DEFAULT_LINUX_SDK_PATH = Path("/home/ubuntu/agora-go-sdk/agora_sdk")


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
    return _start_srt_publish(
        srt_url=srt_url,
        channel=channel,
        publish_uid=publish_uid,
        retry_seconds=retry_seconds,
        app_id=app_id,
        app_cert=app_cert,
        video_mode="yuv",
        source_buffer_seconds=0.0,
    )


def start_srt_original_publish(
    *,
    srt_url: str,
    channel: str,
    publish_uid: int,
    retry_seconds: float,
    source_buffer_seconds: float,
    pcm_listen: str = "",
    video_listen: str = "",
    app_id: str,
    app_cert: str,
) -> subprocess.Popen:
    """Start an original viewer publisher from SRT with a small jitter buffer."""
    return _start_srt_publish(
        srt_url=srt_url,
        channel=channel,
        publish_uid=publish_uid,
        retry_seconds=retry_seconds,
        app_id=app_id,
        app_cert=app_cert,
        video_mode="encoded",
        source_buffer_seconds=source_buffer_seconds,
        pcm_listen=pcm_listen,
        video_listen=video_listen,
    )


def _start_srt_publish(
    *,
    srt_url: str,
    channel: str,
    publish_uid: int,
    retry_seconds: float,
    app_id: str,
    app_cert: str,
    video_mode: str,
    source_buffer_seconds: float,
    pcm_listen: str = "",
    video_listen: str = "",
) -> subprocess.Popen:
    """Start a long-running SRT publisher process."""
    env = os.environ.copy()
    env.setdefault("AGORA_APP_ID", app_id)
    env.setdefault("AGORA_APP_CERT", app_cert)
    env.setdefault("AGORA_APP_CERTIFICATE", app_cert)
    if "DYLD_LIBRARY_PATH" not in env and DEFAULT_SDK_PATH.exists():
        env["DYLD_LIBRARY_PATH"] = str(DEFAULT_SDK_PATH.resolve())
    if "LD_LIBRARY_PATH" not in env and DEFAULT_LINUX_SDK_PATH.exists():
        env["LD_LIBRARY_PATH"] = str(DEFAULT_LINUX_SDK_PATH.resolve())

    cmd = [
        sys.executable, str(ROOT_DIR / "publish_srt_to_agora.py"),
        "--srt-url", srt_url,
        "--channel", channel,
        "--uid", str(publish_uid),
        "--video-mode", video_mode,
        "--source-buffer-seconds", str(source_buffer_seconds),
        "--retry-seconds", str(retry_seconds),
        "--max-attempts", "0",
    ]
    if pcm_listen:
        cmd.extend(["--pcm-listen", pcm_listen])
    if video_listen:
        cmd.extend(["--video-listen", video_listen])
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

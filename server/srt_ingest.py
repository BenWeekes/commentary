"""Helpers for SRT-backed publishing processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
GO_DIR = ROOT_DIR / "go-audio-video-publisher"

# Preferred per-machine SDK location, populated by go-audio-video-publisher/setup-agora-sdk.sh.
SDK_DIR_LINUX = GO_DIR / "agora-sdk" / "agora_sdk"
SDK_DIR_MAC = GO_DIR / "agora-sdk" / "agora_sdk_mac"

# Legacy locations kept as a fallback so existing dev setups still work.
LEGACY_SDK_PATH_MAC = (
    ROOT_DIR.parent / "codex" / "server-custom-llm" / "go-audio-subscriber" / "sdk" / "agora_sdk_mac"
)


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


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
    if "DYLD_LIBRARY_PATH" not in env:
        mac_sdk = _first_existing(SDK_DIR_MAC, LEGACY_SDK_PATH_MAC)
        if mac_sdk:
            env["DYLD_LIBRARY_PATH"] = str(mac_sdk.resolve())
    if "LD_LIBRARY_PATH" not in env:
        linux_sdk = _first_existing(SDK_DIR_LINUX)
        if linux_sdk:
            env["LD_LIBRARY_PATH"] = str(linux_sdk.resolve())

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

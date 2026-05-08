"""Helpers for reading live PCM audio from an SRT source."""

from __future__ import annotations

import os
import signal
import subprocess


def start_srt_audio_pipe(srt_url: str) -> subprocess.Popen:
    """Decode the first SRT audio track to 16kHz mono PCM on stdout."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", srt_url,
        "-map", "0:a:0",
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-f", "s16le",
        "pipe:1",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )


def stop_srt_audio_pipe(proc: subprocess.Popen | None) -> None:
    """Stop an ffmpeg SRT audio pipe process."""
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

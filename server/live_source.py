"""Resolve live match source config into a normalized Agora source."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from server.config import MatchConfig, ServerConfig, get_live_source
from server.srt_ingest import start_srt_ingest


@dataclass
class ResolvedLiveSource:
    source_type: str
    channel: str
    video_uid: int
    atmosphere_uid: int
    commentary_uid: int
    source_atmos_enabled: bool
    owned_proc: subprocess.Popen | None = None


def _log_stream(stream, tag: str):
    if not stream:
        return
    for line in stream:
        text = line.decode(errors="replace").rstrip()
        if text:
            print(f"  [{tag}] {text}")


def _kill_proc(proc: subprocess.Popen | None, tag: str):
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
    print(f"  [{tag}] process killed")


def _wait_for_stdout_signal(
    proc: subprocess.Popen,
    signal_text: str,
    timeout: float,
    stop_event,
    tag: str,
) -> None:
    """Block until a specific stdout line appears or raise on failure/timeout."""
    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise RuntimeError("stopped while waiting for source readiness")
            if proc.poll() is not None:
                raise RuntimeError(f"source ingest exited early with code {proc.returncode}")
            events = sel.select(timeout=0.5)
            if not events:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            text = line.decode(errors="replace").rstrip()
            if text:
                print(f"  [{tag}] {text}")
            if signal_text in text:
                return
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    raise RuntimeError(f"timed out waiting for source readiness signal '{signal_text}'")


def resolve_live_source(
    match_cfg: MatchConfig,
    server_cfg: ServerConfig,
    stop_event,
    tag: str,
) -> ResolvedLiveSource:
    """Resolve a live source and start owned ingest if needed."""
    source = get_live_source(match_cfg)
    if not source:
        raise RuntimeError("live match missing source configuration")

    if source.type == "agora":
        return ResolvedLiveSource(
            source_type="agora",
            channel=source.channel,
            video_uid=source.video_uid,
            atmosphere_uid=source.atmosphere_uid,
            commentary_uid=source.commentary_uid,
            source_atmos_enabled=True,
            owned_proc=None,
        )

    if source.type != "srt":
        raise RuntimeError(f"unsupported live source type '{source.type}'")

    print(f"[{tag}] Starting SRT ingest → Agora channel={source.ingest_channel} uid={source.publish_uid}")
    proc = start_srt_ingest(
        srt_url=source.url,
        channel=source.ingest_channel,
        publish_uid=source.publish_uid,
        retry_seconds=source.retry_seconds,
        app_id=server_cfg.agora_app_id,
        app_cert=server_cfg.agora_app_cert,
    )
    threading.Thread(
        target=_log_stream,
        args=(proc.stderr, f"{tag} SRC err"),
        daemon=True,
    ).start()
    try:
        _wait_for_stdout_signal(
            proc,
            "source publishing started",
            timeout=60.0,
            stop_event=stop_event,
            tag=f"{tag} SRC",
        )
    except Exception:
        _kill_proc(proc, tag=f"{tag} SRC")
        raise

    threading.Thread(
        target=_log_stream,
        args=(proc.stdout, f"{tag} SRC out"),
        daemon=True,
    ).start()

    return ResolvedLiveSource(
        source_type="srt",
        channel=source.ingest_channel,
        video_uid=source.publish_uid,
        atmosphere_uid=0,
        commentary_uid=source.publish_uid,
        source_atmos_enabled=False,
        owned_proc=proc,
    )


def stop_resolved_live_source(resolved: ResolvedLiveSource | None, tag: str) -> None:
    """Stop any owned live-source ingest process."""
    if not resolved or not resolved.owned_proc:
        return
    _kill_proc(resolved.owned_proc, tag=f"{tag} SRC")

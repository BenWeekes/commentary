"""Resolve live match source config into a normalized Agora source."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from server.config import MatchConfig, ServerConfig, get_live_source
from server.srt_ingest import start_srt_ingest, start_srt_original_publish


@dataclass
class ResolvedLiveSource:
    source_type: str
    channel: str
    video_uid: int
    atmosphere_uid: int
    commentary_uid: int
    source_atmos_enabled: bool
    original_channel: str = ""
    source_buffer_seconds: float = 0.0
    local_pcm_addr: str = ""
    local_atmos_pcm_addr: str = ""
    local_video_addr: str = ""
    owned_proc: subprocess.Popen | None = None
    owned_procs: list[tuple[subprocess.Popen, str]] = field(default_factory=list)


_DEMO_SRT_LOCK = threading.Lock()
_DEMO_SRT_BY_PORT: dict[int, subprocess.Popen] = {}


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
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    print(f"  [{tag}] process stopped")


def _build_demo_srt_url(port: int) -> str:
    return f"srt://127.0.0.1:{port}?mode=caller&latency=200000"


def _run_unique_uid(base_uid: int) -> int:
    return 100000 + ((base_uid * 1000 + int(time.time() * 1000)) % 800000)


def _start_demo_srt_loop(source, tag: str) -> tuple[subprocess.Popen, str]:
    """Start one looped local SRT listener for demo-live testing."""
    port = int(source.demo_srt_port)
    srt_url = _build_demo_srt_url(port)
    with _DEMO_SRT_LOCK:
        existing = _DEMO_SRT_BY_PORT.get(port)
        if existing and existing.poll() is None:
            raise RuntimeError(f"demo SRT looper already running on port {port}")
        _DEMO_SRT_BY_PORT.pop(port, None)

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",
            "-stream_loop", "-1",
            "-re",
            "-i", source.demo_media_file,
            "-stream_loop", "-1",
            "-re",
            "-i", source.demo_atmosphere_file,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map", "0:a:0",
            "-c:v", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-c:a", "aac",
            "-ar:a", "16000",
            "-ac:a", "1",
            "-b:a", "64k",
            "-f", "mpegts",
            f"srt://0.0.0.0:{port}?mode=listener&latency=200000",
        ]
        print(f"[{tag}] Starting demo SRT loop on {srt_url}")
        print(f"[{tag}] Demo stream map: #0:0 video, #0:1 atmosphere, #0:2 commentary")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        _DEMO_SRT_BY_PORT[port] = proc

    threading.Thread(
        target=_log_stream,
        args=(proc.stdout, f"{tag} DEMO SRT out"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_log_stream,
        args=(proc.stderr, f"{tag} DEMO SRT err"),
        daemon=True,
    ).start()

    time.sleep(0.75)
    if proc.poll() is not None:
        with _DEMO_SRT_LOCK:
            if _DEMO_SRT_BY_PORT.get(port) is proc:
                _DEMO_SRT_BY_PORT.pop(port, None)
        raise RuntimeError(f"demo SRT looper exited early with code {proc.returncode}")

    return proc, srt_url


def _release_demo_srt_proc(proc: subprocess.Popen, tag: str) -> None:
    with _DEMO_SRT_LOCK:
        for port, current in list(_DEMO_SRT_BY_PORT.items()):
            if current is proc:
                _DEMO_SRT_BY_PORT.pop(port, None)
    _kill_proc(proc, tag=tag)


def _wait_for_stdout_signal(
    proc: subprocess.Popen,
    signal_text: str,
    timeout: float,
    stop_event,
    tag: str,
) -> str:
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
                return text
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    raise RuntimeError(f"timed out waiting for source readiness signal '{signal_text}'")


_LOCAL_READY_RE = re.compile(r"local sources ready pcm=(?P<pcm>\S*)(?: atmos_pcm=(?P<atmos_pcm>\S*))? video=(?P<video>\S*)")


def _parse_local_ready_line(line: str) -> tuple[str, str, str]:
    match = _LOCAL_READY_RE.search(line)
    if not match:
        raise RuntimeError(f"could not parse local source line: {line}")
    return match.group("pcm"), match.group("atmos_pcm") or "", match.group("video")


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
            original_channel=source.channel,
            source_buffer_seconds=0.0,
            owned_proc=None,
        )

    demo_srt_proc: subprocess.Popen | None = None
    if source.type in ("srt_direct", "demo_srt_direct"):
        srt_url = source.url
        publish_uid = source.publish_uid
        if source.type == "demo_srt_direct":
            demo_srt_proc, srt_url = _start_demo_srt_loop(source, tag)
            publish_uid = _run_unique_uid(source.publish_uid)

        print(f"[{tag}] Starting direct SRT original publish → Agora channel={source.original_channel} uid={publish_uid}")
        proc = start_srt_original_publish(
            srt_url=srt_url,
            channel=source.original_channel,
            publish_uid=publish_uid,
            retry_seconds=source.retry_seconds,
            source_buffer_seconds=source.original_buffer_seconds,
            pcm_listen="127.0.0.1:0",
            atmos_pcm_listen="127.0.0.1:0",
            video_listen="127.0.0.1:0",
            audio_stream_index=source.audio_stream_index,
            atmos_audio_stream_index=source.atmosphere_audio_stream_index,
            app_id=server_cfg.agora_app_id,
            app_cert=server_cfg.agora_app_cert,
        )
        threading.Thread(
            target=_log_stream,
            args=(proc.stderr, f"{tag} SRC err"),
            daemon=True,
        ).start()
        try:
            ready_line = _wait_for_stdout_signal(
                proc,
                "local sources ready",
                timeout=30.0,
                stop_event=stop_event,
                tag=f"{tag} SRC",
            )
            local_pcm_addr, local_atmos_pcm_addr, local_video_addr = _parse_local_ready_line(ready_line)
            _wait_for_stdout_signal(
                proc,
                "source publishing started",
                timeout=180.0,
                stop_event=stop_event,
                tag=f"{tag} SRC",
            )
        except Exception:
            _kill_proc(proc, tag=f"{tag} SRC")
            if demo_srt_proc:
                _release_demo_srt_proc(demo_srt_proc, tag=f"{tag} DEMO SRT")
            raise

        threading.Thread(
            target=_log_stream,
            args=(proc.stdout, f"{tag} SRC out"),
            daemon=True,
        ).start()

        return ResolvedLiveSource(
            source_type="srt_direct",
            channel=source.original_channel,
            video_uid=publish_uid,
            atmosphere_uid=0,
            commentary_uid=0,
            source_atmos_enabled=bool(local_atmos_pcm_addr),
            original_channel=source.original_channel,
            source_buffer_seconds=0.0,
            local_pcm_addr=local_pcm_addr,
            local_atmos_pcm_addr=local_atmos_pcm_addr,
            local_video_addr=local_video_addr,
            owned_proc=proc,
            owned_procs=[(demo_srt_proc, f"{tag} DEMO SRT")] if demo_srt_proc else [],
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
        original_channel=source.ingest_channel,
        source_buffer_seconds=0.0,
        owned_proc=proc,
    )


def stop_resolved_live_source(resolved: ResolvedLiveSource | None, tag: str) -> None:
    """Stop any owned live-source ingest process."""
    if not resolved:
        return
    if resolved.owned_proc:
        _kill_proc(resolved.owned_proc, tag=f"{tag} SRC")
    for proc, proc_tag in resolved.owned_procs:
        _release_demo_srt_proc(proc, tag=proc_tag)

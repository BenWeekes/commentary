"""Match worker: orchestrates 1 STT → N language pipelines for a single match."""

import collections
import datetime
import json
import os
import selectors
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

import openai

from lib.audio import load_atmosphere, convert_to_pcm
from lib.constants import ELEVENLABS_MODEL, VIDEO_DELAY_S
from lib.corrections import TERMS_LIST
from lib.events import load_events_file

# ─── Per-match keyterms loading ──────────────────────────────────────────

MATCH_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "match_data")


def _load_match_keyterms(match_id):
    """Load per-match keyterms from match_data/{match_id}/keyterms.txt.

    Returns list of terms, or None if file does not exist.
    One term per line, blank lines and # comments are skipped.
    """
    path = os.path.join(MATCH_DATA_DIR, match_id, "keyterms.txt")
    if not os.path.isfile(path):
        return None
    terms = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return terms if terms else None
from lib.sr_prefetcher import SRPrefetcher
from lib.stt_pipeline import run_stt_pipeline_multi, run_stt_pipeline_live
from lib.translator import translate_text, voice_for_lang, LANG_VOICES
from lib.tts_engine import TTSEngine, _ts

from server.cloud_recording import (
    RecordingSession, start_channel_recording, stop_channel_recording,
)
from server.config import MatchConfig, ServerConfig
from server.live_source import resolve_live_source, stop_resolved_live_source


# ─── Telemetry dataclasses ────────────────────────────────────────────────

@dataclass
class LangTelemetry:
    stt_played: int = 0
    sr_played: int = 0
    sr_cut_short_count: int = 0
    stt_cut_short_count: int = 0
    drop_count: int = 0
    avg_translate_ms: float = 0.0
    avg_tts_ms: float = 0.0
    avg_margin_ms: float = 0.0


@dataclass
class MatchStatus:
    match_id: str
    state: str = "idle"  # idle | starting | running | stopped | error
    stt_utterance_count: int = 0
    languages: dict = field(default_factory=dict)
    error: str | None = None
    started_at: float | None = None


# ─── Language pipeline state ──────────────────────────────────────────────

class _LangPipeline:
    """State for one language within a match."""

    def __init__(self, lang, channel, tts, sr_prefetcher, publisher):
        self.lang = lang
        self.channel = channel
        self.tts = tts
        self.sr_prefetcher = sr_prefetcher
        self.publisher = publisher
        self.video_start = None
        self.telemetry = LangTelemetry()
        self.recent_utterances = collections.deque(maxlen=100)


# ─── Publisher management (reused from live_match.py patterns) ────────────

def _start_publisher(h264_file, channel, video_delay, app_id, app_cert, start_at=None):
    """Launch Go publisher for one language channel.

    Args:
        start_at: optional absolute Unix timestamp for synchronized start.
            When provided, overrides video_delay (Go publisher sleeps until
            this wall-clock time instead of sleeping a relative duration).
    """
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "go-audio-video-publisher")
    sender = os.path.join(base_dir, "reference", "agora_go_sdk", "send_h264_pcm_uid73.go")
    default_sdk_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "codex",
        "server-custom-llm", "go-audio-subscriber", "sdk", "agora_sdk_mac"
    )

    env = os.environ.copy()
    env["AGORA_APP_CERTIFICATE"] = app_cert
    if "DYLD_LIBRARY_PATH" not in env:
        env["DYLD_LIBRARY_PATH"] = os.path.abspath(default_sdk_path)

    abs_h264 = os.path.abspath(h264_file)
    cmd = ["go", "run", sender, app_id, channel, abs_h264, "stdin"]
    if start_at is not None:
        # Absolute Unix timestamp — Go publisher detects >1e9 as absolute
        cmd.append(f"{start_at:.3f}")
    elif video_delay > 0:
        cmd.append(str(video_delay))

    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=base_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    return proc


def _wait_for_publisher_signal(proc, signal_text, timeout, tag):
    """Wait for a specific text in publisher stdout. Returns time.time() when found."""
    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    result_time = None
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=min(remaining, 0.5))
            if not events:
                if proc.poll() is not None:
                    print(f"  [{tag}] WARNING: Publisher exited (code {proc.returncode})")
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            if not text:
                continue
            print(f"  [{tag}] {text}")
            if signal_text in text:
                result_time = time.time()
                break
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    if result_time is None:
        result_time = time.time()
        print(f"  [{tag}] WARNING: '{signal_text}' not received within {timeout}s")

    return result_time


def _wait_for_stderr_signal(proc, signal_text, timeout, tag):
    """Wait for a specific text in process stderr. Returns time.time() when found.

    Same logic as _wait_for_publisher_signal but reads stderr.
    Used for subscribe_audio.go where stdout carries PCM data.
    """
    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stderr, selectors.EVENT_READ)

    result_time = None
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=min(remaining, 0.5))
            if not events:
                if proc.poll() is not None:
                    print(f"  [{tag}] WARNING: Process exited (code {proc.returncode})")
                    break
                continue
            line = proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            if not text:
                continue
            print(f"  [{tag}] {text}")
            if signal_text in text:
                result_time = time.time()
                break
    finally:
        sel.unregister(proc.stderr)
        sel.close()

    if result_time is None:
        result_time = time.time()
        print(f"  [{tag}] WARNING: '{signal_text}' not received within {timeout}s")

    return result_time


def _kill_publisher(proc, tag="PUB"):
    """Kill publisher and all child processes."""
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        print(f"  [{tag}] Publisher killed.")


def _log_pub_stream(stream, tag):
    """Read publisher stream lines in background."""
    for line in stream:
        text = line.decode(errors='replace').rstrip()
        if not text or 'PushVideoEncodedData' in text or 'SESS_CTRL' in text:
            continue
        print(f"  [{tag}] {text}")


# ─── MatchWorker ──────────────────────────────────────────────────────────

class MatchWorker:
    """Manages one match: 1 STT → N languages × (translate → TTS → Go pub)."""

    def __init__(self, match_cfg: MatchConfig, server_cfg: ServerConfig, match_store=None):
        self._match = match_cfg
        self._server = server_cfg
        self._match_store = match_store
        self._stop = threading.Event()
        self._thread = None
        self._pipelines: dict[str, _LangPipeline] = {}
        self._status = MatchStatus(match_id=match_cfg.match_id)
        self._oai_client = None
        self._roster = None
        self._video_start_ref = [None]  # mutable ref for STT
        self._stt_utterance_count = 0
        self._recent_transcript = collections.deque(maxlen=50)
        # Structured log files
        self._log_dir = None
        self._stt_log = None
        self._lang_logs = {}  # lang -> file handle
        self._keyterms = None
        self._telemetry_lock = threading.Lock()
        self._recording_sessions: dict[str, RecordingSession] = {}

    def start(self):
        """Spawn background thread to run the match. Safe to call again after stop()."""
        self._stop = threading.Event()
        self._pipelines = {}
        self._status = MatchStatus(match_id=self._match.match_id)
        self._status.state = "starting"
        self._status.started_at = time.time()
        self._video_start_ref = [None]
        self._stt_utterance_count = 0
        self._recent_transcript = collections.deque(maxlen=50)
        self._log_dir = None
        self._stt_log = None
        self._lang_logs = {}
        self._keyterms = None
        self._recording_sessions = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal stop and wait for cleanup."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    @property
    def status(self) -> MatchStatus:
        self._status.stt_utterance_count = self._stt_utterance_count
        self._status.languages = {}
        for lang, pipe in self._pipelines.items():
            rec_session = self._recording_sessions.get(lang)
            self._status.languages[lang] = {
                "channel": pipe.channel,
                "state": "running" if pipe.tts and not self._stop.is_set() else "stopped",
                "tts_queue_size": pipe.tts.queue_size() if pipe.tts else 0,
                "recording_sid": rec_session.sid if rec_session else None,
                "telemetry": {
                    "stt_played": pipe.telemetry.stt_played,
                    "sr_played": pipe.telemetry.sr_played,
                    "sr_cut_short_count": pipe.telemetry.sr_cut_short_count,
                    "stt_cut_short_count": pipe.telemetry.stt_cut_short_count,
                    "drop_count": pipe.telemetry.drop_count,
                },
            }
        return self._status

    def _run(self):
        """Dispatch to demo or live mode."""
        if self._match.mode == "live":
            self._run_live()
        else:
            self._run_demo()

    def _run_demo(self):
        """Demo mode lifecycle: file-backed match with local Go publishers.

        All language publishers share a single target_start time so video,
        atmosphere, and STT-derived play_at values are synchronized across
        all output channels.
        """
        tag = f"MATCH {self._match.match_id}"
        self._status.state = "starting"
        self._status.started_at = time.time()

        try:
            self._oai_client = openai.OpenAI(api_key=self._server.openai_api_key)

            # Warm up OpenAI — first API call per process takes ~15s (model cold-start).
            # Fire one throwaway translation per language in parallel so the penalty
            # is absorbed during startup instead of delaying the first real utterance.
            warmup_langs = [l for l in self._match.languages if l != "en"]
            if warmup_langs:
                print(f"[{tag}] Warming up OpenAI ({len(warmup_langs)} langs)...")
                warmup_t0 = time.time()
                warmup_threads = []
                for wl in warmup_langs:
                    def _warmup(lang=wl):
                        try:
                            translate_text(self._oai_client, "Kick off.",
                                           lang, model=self._server.translation_model)
                        except Exception:
                            pass
                    t = threading.Thread(target=_warmup, daemon=True)
                    t.start()
                    warmup_threads.append(t)
                for t in warmup_threads:
                    t.join(timeout=20.0)
                print(f"[{tag}] OpenAI warm — {time.time() - warmup_t0:.1f}s")

            # Skip atmosphere for demo — only used in live mode
            atmosphere_pcm = None

            # Try to load roster: prefer match_store (pre-refreshed), then SR API
            self._load_roster(tag)

            # Compute shared target_start: all publishers will begin video
            # at this exact wall-clock time. Margin accounts for sequential
            # publisher launch + Agora connection time.
            n_langs = len(self._match.languages) + 1  # +1 for original audio pipeline
            connection_margin = max(5.0, n_langs * 2.0)
            target_start = time.time() + connection_margin + self._match.video_delay
            print(f"[{tag}] Shared target_start={target_start:.3f} "
                  f"({self._match.video_delay}s delay + {connection_margin:.0f}s margin)")

            # Set video_start_ref immediately — shared across all languages
            self._video_start_ref[0] = target_start

            # Load per-match keyterms (fall back to global TERMS_LIST)
            self._load_keyterms(tag)

            # Set up structured log directory and STT log
            self._setup_log_dir()
            self._open_stt_log(target_start)

            # Start per-language pipelines with shared start_at
            for lang in self._match.languages:
                if self._stop.is_set():
                    break
                self._start_lang_pipeline(lang, atmosphere_pcm, tag, start_at=target_start)

            # Start original audio pipeline in background thread — it does
            # convert_to_pcm + publisher wait which shouldn't eat into the
            # connection margin that translated pipelines need.
            # The background thread builds the pipe into original_result;
            # the main thread inserts it into self._pipelines after wait().
            original_ready = threading.Event()
            original_result = {}  # thread writes {"pipe": ..} or {"error": ..}
            if not self._stop.is_set():
                threading.Thread(
                    target=self._start_original_pipeline,
                    args=(atmosphere_pcm, tag),
                    kwargs={"result": original_result, "ready_event": original_ready},
                    daemon=True).start()

            # Open per-language log files
            for lang, pipe in self._pipelines.items():
                if lang == "original":
                    continue  # no log file for original passthrough
                self._open_lang_log(lang, pipe.tts.voice_id, pipe.video_start)

            if self._stop.is_set():
                return

            self._status.state = "running"
            self._start_recordings(tag)

            # Start STT NOW — processes audio during video delay, giving
            # the translate+TTS pipeline the full delay as head start.
            # video_start_ref is already set to target_start.
            stt_thread = threading.Thread(
                target=self._run_stt, daemon=True)
            stt_thread.start()
            print(f"[{tag}] STT started (processing during "
                  f"{self._match.video_delay}s video delay)")

            # Wait for all publishers' video delay to complete
            actual_vs_values = []
            for lang, pipe in self._pipelines.items():
                if self._stop.is_set():
                    break
                if lang == "original":
                    continue  # original pipeline started in background thread

                pub_tag = f"{tag} {lang.upper()} PUB"
                vs = _wait_for_publisher_signal(
                    pipe.publisher, "video delay complete",
                    timeout=int(connection_margin + self._match.video_delay) + 15,
                    tag=pub_tag)
                actual_spread = abs(vs - target_start)
                if actual_spread > 0.5:
                    print(f"[{tag}] WARNING: {lang} video_start drifted "
                          f"{actual_spread:.3f}s from target")
                print(f"[{tag}] {lang}: video_start={vs:.3f} "
                      f"(target={target_start:.3f}, drift={vs - target_start:+.3f}s)")

                # Update pipeline with actual video_start from publisher
                pipe.video_start = vs
                pipe.tts.video_start = vs
                actual_vs_values.append(vs)

                # Start log reader daemons
                threading.Thread(
                    target=_log_pub_stream,
                    args=(pipe.publisher.stdout, f"{tag} {lang.upper()} out"),
                    daemon=True).start()
                threading.Thread(
                    target=_log_pub_stream,
                    args=(pipe.publisher.stderr, f"{tag} {lang.upper()} err"),
                    daemon=True).start()

            # Update STT scheduling ref to mean actual video_start across languages
            if actual_vs_values:
                mean_vs = sum(actual_vs_values) / len(actual_vs_values)
                self._video_start_ref[0] = mean_vs
                print(f"[{tag}] video_start_ref updated to mean={mean_vs:.3f} "
                      f"(spread={max(actual_vs_values) - min(actual_vs_values):.3f}s)")

            # Always wait for original pipeline thread to finish so we can
            # adopt or kill its publisher — prevents orphaned Go processes.
            original_ready.wait(timeout=30)
            orig_pipe = original_result.get("pipe")
            orig_error = original_result.get("error")
            if orig_error:
                print(f"[{tag}] WARNING: Original pipeline failed: {orig_error}")
            elif orig_pipe and not self._stop.is_set():
                self._pipelines["original"] = orig_pipe
                threading.Thread(
                    target=_log_pub_stream,
                    args=(orig_pipe.publisher.stdout, f"{tag} ORIGINAL out"),
                    daemon=True).start()
                threading.Thread(
                    target=_log_pub_stream,
                    args=(orig_pipe.publisher.stderr, f"{tag} ORIGINAL err"),
                    daemon=True).start()
            elif orig_pipe:
                # Match stopped before we could adopt — kill the orphan
                print(f"[{tag}] Killing orphaned original publisher (match stopped)")
                _kill_publisher(orig_pipe.publisher, tag=f"{tag} ORIGINAL")
            elif not orig_error:
                # Timed out: background thread still running. Mark abandoned so
                # the thread kills its own publisher when it finishes.
                original_result["abandoned"] = True
                print(f"[{tag}] WARNING: Original pipeline timed out — marked abandoned")

            # Register SR events on all prefetchers (needs actual video_start)
            self._register_events(tag)

            # Wait for STT to finish
            stt_thread.join()

            # Drain TTS queues
            print(f"[{tag}] STT done — draining TTS queues...")
            drain_end = time.time() + self._match.video_delay
            while time.time() < drain_end and not self._stop.is_set():
                time.sleep(0.5)

        except Exception as e:
            self._status.state = "error"
            self._status.error = str(e)
            print(f"[{tag}] ERROR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup(tag)

    def _run_live(self):
        """Live mode lifecycle: subscribe to source Agora channel, STT-only (no SR).

        All relay publishers share a single target_start time so video and
        atmosphere are synchronized across all output channels.
        """
        tag = f"MATCH {self._match.match_id} LIVE"
        self._status.state = "starting"
        self._status.started_at = time.time()

        subscribe_proc = None
        live_audio_sock = None
        live_audio_pipe = None
        relay_procs = {}  # lang -> proc
        resolved = None

        try:
            self._oai_client = openai.OpenAI(api_key=self._server.openai_api_key)
            self._load_roster(tag)
            resolved = resolve_live_source(self._match, self._server, self._stop, tag)

            base_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "go-audio-video-publisher")
            default_sdk_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "..", "codex", "server-custom-llm", "go-audio-subscriber",
                "sdk", "agora_sdk_mac")

            env = os.environ.copy()
            env["AGORA_APP_CERTIFICATE"] = self._server.agora_app_cert
            if "DYLD_LIBRARY_PATH" not in env:
                env["DYLD_LIBRARY_PATH"] = os.path.abspath(default_sdk_path)

            live_audio_source = None
            if resolved.source_type == "srt_direct":
                if not resolved.local_pcm_addr or not resolved.local_video_addr:
                    raise RuntimeError("srt_direct source missing local PCM/video endpoints")
                print(f"[{tag}] Connecting STT PCM socket {resolved.local_pcm_addr}")
                pcm_host, pcm_port = resolved.local_pcm_addr.rsplit(":", 1)
                live_audio_sock = socket.create_connection((pcm_host, int(pcm_port)), timeout=10.0)
                live_audio_pipe = live_audio_sock.makefile("rb")
                live_audio_source = live_audio_pipe
            else:
                subscribe_cmd = [
                    "go", "run", "./cmd/subscribe_audio",
                    "--app-id", self._server.agora_app_id,
                    "--channel", resolved.channel,
                    "--uid", str(resolved.commentary_uid),
                ]
                print(f"[{tag}] Starting subscribe_audio on "
                      f"channel={resolved.channel} uid={resolved.commentary_uid}")
                subscribe_proc = subprocess.Popen(
                    subscribe_cmd,
                    env=env, cwd=base_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=os.setsid,
                )
                _wait_for_stderr_signal(
                    subscribe_proc, "audio subscribing started",
                    timeout=15, tag=f"{tag} SUB")
                threading.Thread(
                    target=_log_pub_stream,
                    args=(subscribe_proc.stderr, f"{tag} SUB err"),
                    daemon=True).start()
                live_audio_source = subscribe_proc.stdout

            if self._stop.is_set():
                return

            # Compute shared target_start for all relay publishers
            n_langs = len(self._match.languages)
            connection_margin = max(5.0, n_langs * 2.0)
            relay_delay = max(0.0, self._match.video_delay - resolved.source_buffer_seconds)
            target_start = time.time() + connection_margin + relay_delay
            self._video_start_ref[0] = target_start
            print(f"[{tag}] Shared target_start={target_start:.3f} "
                  f"({relay_delay}s relay delay + {connection_margin:.0f}s margin)")

            # Load per-match keyterms (fall back to global TERMS_LIST)
            self._load_keyterms(tag)

            # Set up structured log directory and STT log
            self._setup_log_dir()
            self._open_stt_log(target_start)

            # --- Start relay_publish.go per language ---
            for lang in self._match.languages:
                if self._stop.is_set():
                    break

                output_channel = f"{self._match.match_id}-{lang}"
                relay_cmd = [
                    "go", "run", "./cmd/relay_publish",
                    "--app-id", self._server.agora_app_id,
                    "--output-channel", output_channel,
                    "--atmos-uid", str(resolved.atmosphere_uid),
                    "--atmos-enabled", "true" if resolved.source_atmos_enabled else "false",
                    "--video-delay", str(relay_delay),
                    "--start-at", f"{target_start:.3f}",
                ]
                if resolved.source_type == "srt_direct":
                    relay_cmd.extend(["--video-source-tcp", resolved.local_video_addr])
                else:
                    relay_cmd.extend([
                        "--source-channel", resolved.channel,
                        "--video-uid", str(resolved.video_uid),
                    ])
                relay_tag = f"{tag} {lang.upper()} RELAY"
                print(f"[{tag}] Starting relay_publish for {lang} → {output_channel}")

                relay_proc = subprocess.Popen(
                    relay_cmd,
                    env=env, cwd=base_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=os.setsid,
                )
                relay_procs[lang] = relay_proc

                # relay_publish signals on stdout (matches existing publisher pattern)
                _wait_for_publisher_signal(
                    relay_proc, "audio publishing started",
                    timeout=15, tag=relay_tag)

                # Start stderr log reader for relay_publish
                threading.Thread(
                    target=_log_pub_stream,
                    args=(relay_proc.stderr, f"{tag} {lang.upper()} RELAY err"),
                    daemon=True).start()

                # Create TTS engine — writes to relay_publish stdin
                def make_telemetry_cb(l=lang):
                    def cb(data):
                        self._on_telemetry(l, data)
                    return cb

                voice_id = voice_for_lang(lang)
                tts = TTSEngine(
                    audio_pipe=relay_proc.stdin,
                    voice_id=voice_id,
                    api_key=self._server.elevenlabs_api_key,
                    on_telemetry=make_telemetry_cb(),
                )

                # Shared target_start for all languages
                tts.video_start = target_start
                tts.start()

                pipe = _LangPipeline(lang, output_channel, tts, sr_prefetcher=None,
                                     publisher=relay_proc)
                pipe.video_start = target_start
                self._pipelines[lang] = pipe

            # Open per-language log files
            for lang, pipe in self._pipelines.items():
                self._open_lang_log(lang, pipe.tts.voice_id, pipe.video_start)

            if self._stop.is_set():
                return

            self._status.state = "running"
            self._start_recordings(tag)

            print(f"[{tag}] Starting STT from live audio source...")
            stt_thread = threading.Thread(
                target=self._run_stt_live,
                args=(live_audio_source,),
                daemon=True)
            stt_thread.start()

            # Wait for relay_publish video delay to complete
            actual_vs_values = []
            for lang, pipe in self._pipelines.items():
                if self._stop.is_set():
                    break
                relay_tag = f"{tag} {lang.upper()} RELAY"
                vs = _wait_for_publisher_signal(
                    pipe.publisher, "video delay complete",
                    timeout=int(connection_margin + relay_delay) + 15,
                    tag=relay_tag)
                drift = abs(vs - target_start)
                if drift > 0.5:
                    print(f"[{tag}] WARNING: {lang} video_start drifted {drift:.3f}s from target")
                print(f"[{tag}] {lang}: video_start={vs:.3f} "
                      f"(target={target_start:.3f}, drift={vs - target_start:+.3f}s)")

                # Update pipeline with actual video_start from publisher
                pipe.video_start = vs
                pipe.tts.video_start = vs
                actual_vs_values.append(vs)

                # Start stdout log reader (after we've consumed signals)
                threading.Thread(
                    target=_log_pub_stream,
                    args=(pipe.publisher.stdout, f"{tag} {lang.upper()} RELAY out"),
                    daemon=True).start()

            # Update STT scheduling ref to mean actual video_start across languages
            if actual_vs_values:
                mean_vs = sum(actual_vs_values) / len(actual_vs_values)
                self._video_start_ref[0] = mean_vs
                print(f"[{tag}] video_start_ref updated to mean={mean_vs:.3f} "
                      f"(spread={max(actual_vs_values) - min(actual_vs_values):.3f}s)")

            # Wait for STT to finish (pipe closes when source audio ends)
            stt_thread.join()

            # Drain TTS queues
            print(f"[{tag}] STT done — draining TTS queues...")
            drain_end = time.time() + max(self._match.video_delay, relay_delay)
            while time.time() < drain_end and not self._stop.is_set():
                time.sleep(0.5)

        except Exception as e:
            self._status.state = "error"
            self._status.error = str(e)
            print(f"[{tag}] ERROR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Kill subscribe_audio
            if live_audio_pipe:
                try:
                    live_audio_pipe.close()
                except Exception:
                    pass
            if live_audio_sock:
                try:
                    live_audio_sock.close()
                except Exception:
                    pass
            if subscribe_proc:
                _kill_publisher(subscribe_proc, tag=f"{tag} SUB")
            # Kill relay publishers and TTS engines
            self._cleanup(tag)
            stop_resolved_live_source(resolved, tag)

    def _start_lang_pipeline(self, lang, atmosphere_pcm, tag, start_at=None):
        """Start Go publisher + TTSEngine + SRPrefetcher for one language.

        Args:
            start_at: shared absolute Unix timestamp for synchronized video start.
                All language publishers in a match receive the same value so they
                begin publishing video at the same wall-clock instant.
        """
        channel = f"{self._match.match_id}-{lang}"
        pub_tag = f"{tag} {lang.upper()} PUB"

        print(f"[{tag}] Starting {lang} pipeline on channel={channel}")
        pub = _start_publisher(
            self._match.video_h264, channel,
            self._match.video_delay,
            self._server.agora_app_id, self._server.agora_app_cert,
            start_at=start_at)

        # Wait for audio ready
        _wait_for_publisher_signal(pub, "audio publishing started", timeout=15, tag=pub_tag)

        # Create TTS engine with telemetry callback
        def make_telemetry_cb(l=lang):
            def cb(data):
                self._on_telemetry(l, data)
            return cb

        voice_id = voice_for_lang(lang)
        tts = TTSEngine(
            audio_pipe=pub.stdin,
            voice_id=voice_id,
            api_key=self._server.elevenlabs_api_key,
            on_telemetry=make_telemetry_cb(),
        )

        if atmosphere_pcm:
            tts.set_atmosphere(atmosphere_pcm)
            tts.set_atmosphere_enabled(True)

        # Use shared start_at as video_start if provided
        video_start = start_at if start_at else time.time() + self._match.video_delay
        tts.video_start = video_start
        tts.start()

        # Create SR prefetcher
        sr_pf = SRPrefetcher(
            tts_engine=tts,
            api_key=self._server.elevenlabs_api_key,
            model=ELEVENLABS_MODEL,
        )
        sr_pf.start()

        pipe = _LangPipeline(lang, channel, tts, sr_pf, pub)
        pipe.video_start = video_start
        self._pipelines[lang] = pipe

    def _start_original_pipeline(self, atmosphere_pcm, tag, start_at=None,
                                 result=None, ready_event=None):
        """Start a passthrough pipeline that plays original audio on {match_id}-original.

        Demo mode: loads original audio PCM from the audio file, starts a Go
        publisher with NO video delay — original channel plays ahead of
        translated channels (video_delay seconds ahead), with A/V in sync.

        Called from a background thread. Writes the built _LangPipeline into
        result["pipe"] (or result["error"] on failure). Does NOT mutate
        self._pipelines — the main thread inserts after ready_event is set.
        """
        pub = None
        try:
            import wave as _wave

            channel = f"{self._match.match_id}-original"
            pub_tag = f"{tag} ORIGINAL PUB"

            print(f"[{tag}] Starting original pipeline on channel={channel} (no delay)")

            # Load original audio as PCM bytes
            pcm_path = convert_to_pcm(self._match.audio)
            with _wave.open(pcm_path, 'rb') as wf:
                original_pcm = wf.readframes(wf.getnframes())
            os.unlink(pcm_path)
            print(f"[{tag}] Original audio: {len(original_pcm)/32000:.1f}s loaded")

            if self._stop.is_set():
                return

            # No video delay — original plays in real time, ahead of translated channels
            pub = _start_publisher(
                self._match.video_h264, channel,
                0,  # no video delay
                self._server.agora_app_id, self._server.agora_app_cert,
                start_at=None)  # no synchronized start — begin immediately

            _wait_for_publisher_signal(pub, "audio publishing started", timeout=15, tag=pub_tag)

            tts = TTSEngine(
                audio_pipe=pub.stdin,
                voice_id="original",
                api_key="",
                on_telemetry=None,
            )

            if atmosphere_pcm:
                tts.set_atmosphere(atmosphere_pcm)
                tts.set_atmosphere_enabled(True)

            # video_start is now (no delay) — audio position starts from beginning
            video_start = time.time()
            tts.video_start = video_start
            tts.set_original_audio(original_pcm)
            tts.set_original_enabled(True)
            tts.start()

            pipe = _LangPipeline("original", channel, tts, sr_prefetcher=None, publisher=pub)
            pipe.video_start = video_start
            if result is not None:
                if result.get("abandoned"):
                    # Main thread timed out waiting — kill our publisher
                    print(f"[{tag}] Original pipeline finished but was abandoned — killing publisher")
                    _kill_publisher(pub, tag=f"{tag} ORIGINAL")
                    pub = None
                else:
                    result["pipe"] = pipe
                    pub = None  # ownership transferred to pipe — don't kill in except
        except Exception as e:
            print(f"[{tag}] ERROR in original pipeline: {e}")
            import traceback
            traceback.print_exc()
            if pub is not None:
                _kill_publisher(pub, tag=f"{tag} ORIGINAL")
            if result is not None:
                result["error"] = str(e)
        finally:
            if ready_event:
                ready_event.set()

    # ─── Data loading ───────────────────────────────────────────────────

    def _load_keyterms(self, tag):
        """Load keyterms via match_store (preferred) or module-level fallback."""
        if self._match_store:
            self._keyterms = self._match_store.read_keyterms(self._match.match_id)
        else:
            self._keyterms = _load_match_keyterms(self._match.match_id)
        if self._keyterms:
            print(f"[{tag}] Loaded {len(self._keyterms)} keyterms for {self._match.match_id}")
        else:
            self._keyterms = TERMS_LIST
            print(f"[{tag}] Using global TERMS_LIST ({len(self._keyterms)} terms)")

    def _load_roster(self, tag):
        """Load roster: prefer match_store (pre-refreshed), then SR API fetch."""
        if self._match_store:
            roster_data = self._match_store.read_roster(self._match.match_id)
            if roster_data and roster_data.get("roster_text"):
                self._roster = roster_data["roster_text"]
                print(f"[{tag}] Loaded roster from match_store")
                return
        # Fall back to live SR API fetch
        self._roster = self._fetch_roster()

    # ─── Structured log files ────────────────────────────────────────────

    def _setup_log_dir(self):
        if self._match_store:
            self._log_dir = self._match_store.get_run_dir(self._match.match_id)
        else:
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._log_dir = os.path.join("logs", f"{self._match.match_id}_{ts}")
            os.makedirs(self._log_dir, exist_ok=True)

    def _open_stt_log(self, target_start):
        path = os.path.join(self._log_dir, "stt.jsonl")
        self._stt_log = open(path, "w", buffering=1)
        roster_list = []
        if self._roster:
            roster_list = [line.lstrip("- ").strip()
                           for line in self._roster.split("\n") if line.strip()]
        header = {
            "type": "header",
            "match_id": self._match.match_id,
            "mode": self._match.mode,
            "started_at": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "video_delay": self._match.video_delay,
            "target_start": target_start,
            "languages": list(self._match.languages),
            "keyterms": list(self._keyterms) if self._keyterms else [],
            "roster": roster_list,
        }
        self._stt_log.write(json.dumps(header) + "\n")
        self._stt_log.flush()

    def _open_lang_log(self, lang, voice_id, video_start):
        path = os.path.join(self._log_dir, f"{lang}.jsonl")
        fh = open(path, "w", buffering=1)
        header = {
            "type": "header",
            "match_id": self._match.match_id,
            "language": lang,
            "voice_id": voice_id,
            "video_start": video_start,
        }
        fh.write(json.dumps(header) + "\n")
        fh.flush()
        self._lang_logs[lang] = fh

    # ─── SR event registration ────────────────────────────────────────────

    def _register_events(self, tag):
        """Register SR events on all language prefetchers."""
        if not self._match.events:
            return

        events = load_events_file(self._match.events)
        if not events:
            print(f"[{tag}] No events in {self._match.events}")
            return

        print(f"[{tag}] Registering {len(events)} events on {len(self._pipelines)} languages")

        for lang, pipe in self._pipelines.items():
            if not pipe.sr_prefetcher:
                continue  # skip original passthrough pipeline
            match_time_start = pipe.video_start - self._match.events_offset

            def make_translate_fn_factory(target_lang=lang):
                def factory():
                    def translate(text):
                        vid = voice_for_lang(target_lang)
                        if target_lang == "en":
                            return (text, vid)
                        return (translate_text(
                            self._oai_client, text, target_lang,
                            model=self._server.translation_model,
                            roster=self._roster), vid)
                    return translate
                return factory

            event_list = []
            for offset, priority, message in events:
                play_at = match_time_start + offset
                event_list.append((message, play_at, make_translate_fn_factory()))

            pipe.sr_prefetcher.set_events(event_list)

            # Start interrupt watcher thread for this language
            threading.Thread(
                target=self._interrupt_watcher,
                args=(lang, pipe, events, match_time_start, tag),
                daemon=True).start()

    def _interrupt_watcher(self, lang, pipe, events, match_time_start, tag):
        """Watch for INTERRUPT events and clear stale SR items (not STT)."""
        for offset, priority, message in events:
            if self._stop.is_set():
                break
            if priority != "INTERRUPT":
                continue

            # Wait until this event's match time
            while not self._stop.is_set():
                match_elapsed = time.time() - match_time_start
                if offset <= match_elapsed:
                    break
                time.sleep(0.1)

            if self._stop.is_set():
                break

            print(f"  [{_ts(pipe.tts.video_start)}] [{tag} {lang.upper()} SR INT] "
                  f"Cancelling stale SR events before offset {offset}")
            interrupt_play_at = match_time_start + offset
            pipe.sr_prefetcher.cancel_before(interrupt_play_at)

    def _run_stt_live(self, audio_pipe):
        """Run STT pipeline from a live audio pipe (subscribe_audio stdout)."""
        self._stt_utterance_count = run_stt_pipeline_live(
            audio_pipe=audio_pipe,
            on_utterance=self._on_utterance,
            deepgram_key=self._server.deepgram_api_key,
            stop_event=self._stop,
            video_start_ref=self._video_start_ref,
            video_delay=self._match.video_delay,
            max_stt_duration=self._match.max_stt_duration,
            keyterms=self._keyterms,
            corrections=[],  # no static corrections for live — keyterms handle recognition
        )

    def _run_stt(self):
        """Run STT pipeline with fan-out to all languages (demo mode)."""
        self._stt_utterance_count = run_stt_pipeline_multi(
            audio_path=self._match.audio,
            on_utterance=self._on_utterance,
            deepgram_key=self._server.deepgram_api_key,
            stop_event=self._stop,
            video_start_ref=self._video_start_ref,
            video_delay=self._match.video_delay,
            max_stt_duration=self._match.max_stt_duration,
            keyterms=self._keyterms,
        )

    @property
    def recent_transcript(self) -> list[dict]:
        """Return recent English STT utterances as list of {text, ts}."""
        return list(self._recent_transcript)

    def _on_utterance(self, text, audio_start, audio_end, play_at):
        """Fan out a corrected STT utterance to all language pipelines."""
        self._stt_utterance_count += 1
        self._recent_transcript.append({
            "text": text,
            "ts": time.time(),
            "audio_start": audio_start,
        })
        # Write STT log line
        if self._stt_log:
            try:
                line = {
                    "type": "utterance",
                    "audio_start": round(audio_start, 2),
                    "audio_end": round(audio_end, 2),
                    "wall_clock": time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
                    "play_at": play_at,
                    "text": text,
                }
                self._stt_log.write(json.dumps(line) + "\n")
                self._stt_log.flush()
            except Exception:
                pass
        for lang, pipe in self._pipelines.items():
            if lang == "original":
                continue  # original pipeline plays file audio, not TTS

            def make_translate_fn(target_lang=lang):
                def translate(t):
                    vid = voice_for_lang(target_lang)
                    if target_lang == "en":
                        return (t, vid)
                    return (translate_text(
                        self._oai_client, t, target_lang,
                        model=self._server.translation_model,
                        roster=self._roster), vid)
                return translate

            # Use per-language video_start for accurate play_at timing
            lang_play_at = (pipe.video_start + audio_start) if pipe.video_start else play_at
            pipe.tts.speak(text, play_at=lang_play_at, translate_fn=make_translate_fn())

    def _on_telemetry(self, lang, data):
        """Process telemetry from a TTSEngine pipe writer.

        Called from multiple threads (pipe_writer, SR scheduler) — all
        counter increments and log writes are protected by _telemetry_lock.
        """
        pipe = self._pipelines.get(lang)
        if not pipe:
            return

        source = data.get("source", "")
        status = data.get("status", "")
        interrupted = data.get("interrupted", False)
        interrupted_by = data.get("interrupted_by", "")

        with self._telemetry_lock:
            # Status-aware counter increments
            if status in ("played", "interrupted"):
                if source == "stt":
                    pipe.telemetry.stt_played += 1
                    if status == "interrupted":
                        pipe.telemetry.stt_cut_short_count += 1
                        print(f"  [TELEMETRY] ERROR: {lang} STT was interrupted by {interrupted_by} "
                              f"— stt_cut_short_count={pipe.telemetry.stt_cut_short_count}")
                elif source == "sr":
                    pipe.telemetry.sr_played += 1
                    if status == "interrupted":
                        pipe.telemetry.sr_cut_short_count += 1
            elif status in ("dropped", "suppressed", "replaced"):
                pipe.telemetry.drop_count += 1

            pipe.recent_utterances.append(data)

            # Write lang log line
            lang_log = self._lang_logs.get(lang)
            if lang_log:
                try:
                    play_at = data.get("play_at")
                    audio_start = None
                    if play_at and pipe.video_start:
                        audio_start = round(play_at - pipe.video_start, 2)
                    xlat_time = data.get("translate_time")
                    tts_time = data.get("tts_time")
                    play_started_at = data.get("play_started_at")
                    play_ended_at = data.get("play_ended_at")
                    start_lag_ms = None
                    if play_started_at and play_at:
                        start_lag_ms = round((play_started_at - play_at) * 1000)
                    line = {
                        "type": "utterance",
                        "source": source,
                        "uid": data.get("uid"),
                        "audio_start": audio_start,
                        "play_at": play_at,
                        "play_started_at": play_started_at,
                        "play_ended_at": play_ended_at,
                        "start_lag_ms": start_lag_ms,
                        "xlat_ms": round(xlat_time * 1000) if xlat_time else None,
                        "tts_ms": round(tts_time * 1000) if tts_time else None,
                        "status": status,
                        "original": data.get("text"),
                        "translated": data.get("translated"),
                        "play_duration_ms": data.get("actual_play_duration_ms", 0),
                        "total_buffered_ms": data.get("total_buffered_ms", 0),
                        "pre_translated": data.get("pre_translated", False),
                        "queue_wait_ms": data.get("queue_wait_ms", 0),
                    }
                    lang_log.write(json.dumps(line) + "\n")
                    lang_log.flush()
                except Exception:
                    pass

    def _fetch_roster(self):
        """Fetch player roster from Sportradar lineups API. Non-fatal on failure."""
        if not self._server.sportradar_api_key or not self._match.sport_event_id:
            return None
        try:
            import urllib.request
            import json
            url = (f"https://api.sportradar.com/soccer-extended/trial/v4/en/"
                   f"sport_events/{self._match.sport_event_id}/lineups.json")
            req = urllib.request.Request(url, headers={
                "x-api-key": self._server.sportradar_api_key,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            names = []
            for team in data.get("sport_event", {}).get("competitors", []):
                for player in team.get("players", []):
                    names.append(player.get("name", ""))
            roster = "\n".join(f"- {n}" for n in names if n)
            if roster:
                print(f"[MATCH {self._match.match_id}] Loaded roster: {len(names)} players")
            return roster or None
        except Exception as e:
            print(f"[MATCH {self._match.match_id}] Roster fetch failed (non-fatal): {e}")
            return None

    # ─── Cloud Recording ─────────────────────────────────────────────────

    def _start_recordings(self, tag):
        """Start cloud recording for each language channel (non-fatal)."""
        cr = self._server.cloud_recording
        if not cr or not self._server.agora_customer_key:
            return

        recording_uid = 800000
        for lang, pipe in self._pipelines.items():
            if lang == "original":
                continue
            try:
                session = start_channel_recording(
                    app_id=self._server.agora_app_id,
                    app_cert=self._server.agora_app_cert,
                    customer_key=self._server.agora_customer_key,
                    customer_secret=self._server.agora_customer_secret,
                    channel=pipe.channel,
                    recording_uid=recording_uid,
                    storage_config=cr,
                )
                self._recording_sessions[lang] = session
                print(f"[{tag}] Recording started for {pipe.channel} "
                      f"(sid={session.sid})")
            except Exception as e:
                print(f"[{tag}] WARNING: Recording start failed for "
                      f"{pipe.channel} (non-fatal): {e}")
            recording_uid += 1

    def _stop_recordings(self, tag):
        """Stop all active cloud recording sessions (non-fatal)."""
        if not self._recording_sessions:
            return

        for lang, session in self._recording_sessions.items():
            try:
                resp = stop_channel_recording(
                    app_id=self._server.agora_app_id,
                    customer_key=self._server.agora_customer_key,
                    customer_secret=self._server.agora_customer_secret,
                    session=session,
                )
                upload_status = resp.get("serverResponse", {}).get(
                    "uploadingStatus", "unknown")
                print(f"[{tag}] Recording stopped for {session.channel} "
                      f"(upload={upload_status})")
            except Exception as e:
                print(f"[{tag}] WARNING: Recording stop failed for "
                      f"{session.channel} (non-fatal): {e}")
        self._recording_sessions.clear()

    def _cleanup(self, tag):
        """Stop all pipelines, kill publishers, and close log files."""
        print(f"[{tag}] Cleaning up...")
        self._stop_recordings(tag)
        for lang, pipe in self._pipelines.items():
            if pipe.sr_prefetcher:
                try:
                    pipe.sr_prefetcher.stop()
                except Exception:
                    pass
            try:
                pipe.tts.stop()
            except Exception:
                pass
            _kill_publisher(pipe.publisher, tag=f"{tag} {lang.upper()}")

        # Close structured log files
        if self._stt_log:
            try:
                self._stt_log.close()
            except Exception:
                pass
            self._stt_log = None
        for lang, fh in self._lang_logs.items():
            try:
                fh.close()
            except Exception:
                pass
        self._lang_logs.clear()

        if self._status.state != "error":
            self._status.state = "stopped"
        self._pipelines.clear()

#!/usr/bin/env python3
"""
Live Match Orchestrator — STT + ElevenLabs TTS + Video Publisher (no avatar agent)

Captures live commentary audio, translates it in real-time via Deepgram STT +
GPT-4o-mini, speaks it via ElevenLabs WebSocket TTS, and publishes both the
TTS audio and match video to the same Agora channel using the Go publisher.

Architecture:
  ┌─────────────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐
  │ Audio source │──▶ │ Deepgram │──▶ │ Correct  │──▶ │ Translate │
  │ (mic/file)  │    │ Nova-3   │    │ (determ.) │    │ GPT-4o-m  │
  └─────────────┘    └──────────┘    └──────────┘    └─────┬─────┘
                                                           │
  ┌─────────────┐    ┌──────────┐                          │
  │ Sportradar  │──▶ │ Translate│──────────────────────────┤
  │ events file │    │ GPT-4o-m │                          │
  └─────────────┘    └──────────┘                          ▼
                                                    ┌──────────────┐
                                                    │ ElevenLabs   │
                                                    │ WebSocket TTS│
                                                    │ (pcm_16000)  │
                                                    └──────┬───────┘
                                                           │ PCM bytes
  ┌─────────────┐                                          ▼
  │ Video file  │──▶ Go publisher ◀─── PCM via stdin ──▶ Agora channel
  │ (.h264)     │    (UID 73, 3s delayed video + TTS audio)
  └─────────────┘

No avatar agent needed. We call ElevenLabs directly and pipe PCM audio
into the Go publisher's stdin alongside the delayed video.

Usage:
    # Full demo with video + STT + SR fallback:
    python3 live_match.py \\
        --audio bmg_fch_first_5min.mp3 \\
        --video-h264 encoded_assets/bundesliga.h264 \\
        --events bmg_fch_md28_full_match.txt \\
        --lang es --channel sportradar-live

    # STT only (TTS to local speakers, no video):
    python3 live_match.py \\
        --audio bmg_fch_first_5min.mp3 \\
        --lang es
"""

import argparse
import json
import os
import re as _re_module
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import openai

from tokens import AccessToken, ServiceRtc

from lib.constants import SAMPLE_RATE, VIDEO_DELAY_S, ELEVENLABS_MODEL
from lib.translator import LANG_NAMES, voice_for_lang, translate_text
from lib.audio import load_atmosphere, convert_to_pcm, pcm_chunks_realtime
from lib.tts_engine import TTSEngine, _ts
from lib.sr_prefetcher import SRPrefetcher
from lib.stt_pipeline import run_stt_pipeline
from lib.events import load_events_file

# ─── Load .env ───────────────────────────────────────────────────────────

def _load_dotenv(path=None):
    """Load key=value pairs from .env file into os.environ."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────

AGORA_APP_ID = os.environ.get("AGORA_APP_ID", "")
AGORA_APP_CERT = os.environ.get("AGORA_APP_CERT", "")
# ElevenLabs
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

# Sportradar
SPORTRADAR_API_KEY = os.environ.get("SPORTRADAR_API_KEY", "")

def get_current_lang(lang_file, default_lang):
    """Read the current language from the lang file, falling back to default."""
    try:
        with open(lang_file) as f:
            code = f.read().strip().lower()
            if code and code in LANG_NAMES:
                return code
    except (FileNotFoundError, OSError):
        pass
    return default_lang


# ─── Session management ──────────────────────────────────────────────────

def _generate_viewer_token(channel, uid, expire_s=3600):
    """Generate an Agora v007 token for a viewer to join a channel."""
    token = AccessToken(AGORA_APP_ID, AGORA_APP_CERT, expire=expire_s)
    rtc = ServiceRtc(channel, uid)
    rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, expire_s)
    token.add_service(rtc)
    return token.build()


class Session:
    """One viewer's session: channel, token, lang file, pipeline state."""

    def __init__(self, lang="es"):
        self.id = uuid.uuid4().hex
        self.channel = f"commentary-{self.id[:8]}"
        self.viewer_uid = 1000 + (hash(self.id) % 9000)
        self.token = _generate_viewer_token(self.channel, self.viewer_uid)
        self.lang = lang
        self.lang_file = f"/tmp/commentary_lang_{self.id}"
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.pipeline_running = False
        self.pipeline_thread = None
        self.sr_prefetcher = None  # set by pipeline when SR events are active
        self.tts_engine = None     # set by pipeline for atmosphere API
        self.created_at = time.time()
        self.last_activity = time.time()
        # Write initial language
        with open(self.lang_file, "w") as f:
            f.write(lang)

    def cleanup(self):
        try:
            os.unlink(self.lang_file)
        except OSError:
            pass


class SessionManager:
    """Manages multiple concurrent viewer sessions."""

    EXPIRE_S = 1800  # 30 min inactivity

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        # Start reaper thread
        threading.Thread(target=self._reaper, daemon=True).start()

    def create(self, lang="es"):
        session = Session(lang=lang)
        with self._lock:
            self._sessions[session.id] = session
        print(f"[SESSION] Created {session.id[:8]} — channel={session.channel}")
        return session

    def get(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.last_activity = time.time()
        return session

    def remove(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.stop_event.set()
            session.cleanup()
            print(f"[SESSION] Removed {session_id[:8]}")

    def _reaper(self):
        """Remove expired sessions every 60s."""
        while True:
            time.sleep(60)
            now = time.time()
            expired = []
            with self._lock:
                for sid, s in self._sessions.items():
                    if not s.pipeline_running and (now - s.last_activity) > self.EXPIRE_S:
                        expired.append(sid)
            for sid in expired:
                print(f"[SESSION] Expiring {sid[:8]} (idle)")
                self.remove(sid)


class ControlHandler(BaseHTTPRequestHandler):
    """HTTP handler for multi-session viewer control.

    Routes:
      POST /api/session                     → create session
      GET  /api/session/{id}/start          → start pipeline
      GET  /api/session/{id}/stop           → stop pipeline
      GET  /api/session/{id}/set-lang?lang= → change language
      GET  /api/session/{id}/status         → poll status
    """
    session_mgr = None  # set before server starts
    args = None         # CLI args — set before server starts
    h264_file = None
    oai_client = None
    atmosphere_pcm = None  # raw PCM bytes for atmosphere mixing
    original_pcm = None    # raw PCM bytes for original commentary pass-through

    # Regex to match /api/session/{id}/{action}
    _SESSION_RE = _re_module.compile(r'^/api/session/([a-f0-9]+)/([\w-]+)$')

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/session":
            lang = qs.get("lang", ["es"])[0].lower()
            if lang not in LANG_NAMES:
                lang = "es"
            session = self.session_mgr.create(lang=lang)
            self._respond(200, {
                "sessionId": session.id,
                "channel": session.channel,
                "token": session.token,
                "uid": session.viewer_uid,
                "appid": AGORA_APP_ID,
                "videoDelay": ControlHandler.args.video_delay if ControlHandler.args else 0,
            })
            return

        # Check for session action routes via POST too
        m = self._SESSION_RE.match(parsed.path)
        if m:
            self._handle_session_action(m.group(1), m.group(2), qs)
            return

        self._respond(404, {"error": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # Serve viewer.html
        if parsed.path in ("/", "/viewer.html"):
            viewer_path = os.path.join(os.path.dirname(__file__) or ".", "viewer.html")
            try:
                with open(viewer_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._respond(404, {"error": "viewer.html not found"})
            return

        m = self._SESSION_RE.match(parsed.path)
        if m:
            self._handle_session_action(m.group(1), m.group(2), qs)
            return

        self._respond(404, {"error": "not found"})

    def _handle_session_action(self, session_id, action, qs):
        session = self.session_mgr.get(session_id)
        if not session:
            self._respond(404, {"error": "session not found"})
            return

        if action == "start":
            if session.pipeline_running:
                self._respond(200, {"status": "already_running"})
            else:
                session.pipeline_running = True  # Set before spawn to prevent race
                session.stop_event.clear()
                session.start_event.set()
                # Spawn pipeline thread for this session
                t = threading.Thread(
                    target=self._run_session_pipeline,
                    args=(session,),
                    daemon=True,
                )
                t.start()
                session.pipeline_thread = t
                self._respond(200, {"status": "starting"})

        elif action == "stop":
            if session.pipeline_running:
                session.stop_event.set()
                self._respond(200, {"status": "stopping"})
            else:
                self._respond(200, {"status": "not_running"})

        elif action == "set-lang":
            lang = qs.get("lang", ["es"])[0].lower()
            try:
                with open(session.lang_file, "w") as f:
                    f.write(lang)
                # Flush queued STT utterances so they don't play in old language
                if hasattr(session, 'tts_engine') and session.tts_engine:
                    tts = session.tts_engine
                    while not tts._text_queue.empty():
                        try:
                            tts._text_queue.get_nowait()
                        except Exception:
                            break
                    # Also discard lookahead (translated in old language)
                    with tts._lookahead_lock:
                        tts._lookahead_buf.clear()
                    tts._lookahead_item = None
                    tts._lang_version += 1
                # Flush prefetched SR events so they re-translate in new language
                if session.sr_prefetcher:
                    session.sr_prefetcher.flush()
                self._respond(200, {"lang": lang})
            except OSError as e:
                self._respond(500, {"error": str(e)})

        elif action == "set-atmosphere":
            enabled = qs.get("enabled", ["false"])[0].lower() == "true"
            if hasattr(session, 'tts_engine') and session.tts_engine:
                session.tts_engine.set_atmosphere_enabled(enabled)
            self._respond(200, {"atmosphere": enabled})

        elif action == "set-original":
            enabled = qs.get("enabled", ["false"])[0].lower() == "true"
            if hasattr(session, 'tts_engine') and session.tts_engine:
                if enabled and not session.tts_engine._original_pcm:
                    self._respond(400, {"error": "no original audio loaded"})
                    return
                session.tts_engine.set_original_enabled(enabled)
            self._respond(200, {"original": enabled})

        elif action == "status":
            lang = "es"
            try:
                with open(session.lang_file) as f:
                    lang = f.read().strip()
            except OSError:
                pass
            atmos = False
            orig = False
            if hasattr(session, 'tts_engine') and session.tts_engine:
                atmos = session.tts_engine._atmosphere_on
                orig = session.tts_engine._original_on
            self._respond(200, {
                "running": session.pipeline_running,
                "lang": lang,
                "atmosphere": atmos,
                "original": orig,
            })

        else:
            self._respond(404, {"error": f"unknown action: {action}"})

    @staticmethod
    def _run_session_pipeline(session):
        """Run the pipeline for a single session in its own thread."""
        args = ControlHandler.args
        h264_file = ControlHandler.h264_file
        oai_client = ControlHandler.oai_client

        session.start_event.wait()
        session.start_event.clear()

        print(f"[SESSION {session.id[:8]}] Starting pipeline on channel={session.channel}")
        run_pipeline_for_session(
            session, args, h264_file, oai_client,
        )
        print(f"[SESSION {session.id[:8]}] Pipeline stopped.")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_control_server(port, session_mgr, args, h264_file, oai_client,
                         atmosphere_pcm=None, original_pcm=None):
    """Start the control HTTP server in a daemon thread."""
    ControlHandler.session_mgr = session_mgr
    ControlHandler.args = args
    ControlHandler.h264_file = h264_file
    ControlHandler.oai_client = oai_client
    ControlHandler.atmosphere_pcm = atmosphere_pcm
    ControlHandler.original_pcm = original_pcm
    server = HTTPServer(("0.0.0.0", port), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[CTL] Control server on http://localhost:{port}")
    print(f"      POST /api/session  →  create session")
    print(f"      GET  /api/session/{{id}}/start|stop|set-lang|status")
    return server


# TTSEngine, _ts imported from lib.tts_engine
# SRPrefetcher imported from lib.sr_prefetcher
# load_atmosphere, convert_to_pcm, pcm_chunks_realtime imported from lib.audio
# run_stt_pipeline imported from lib.stt_pipeline


# ─── Video + Audio publisher ────────────────────────────────────────────

def start_publisher(h264_file, channel, video_delay=0):
    """
    Launch Go publisher that reads H.264 video from file and
    PCM audio from stdin. video_delay seconds before sending video frames.
    """
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "go-audio-video-publisher")
    sender = os.path.join(base_dir, "reference", "agora_go_sdk", "send_h264_pcm_uid73.go")
    default_sdk_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "codex",
        "server-custom-llm", "go-audio-subscriber", "sdk", "agora_sdk_mac"
    )

    env = os.environ.copy()
    env["AGORA_APP_CERTIFICATE"] = AGORA_APP_CERT
    if "DYLD_LIBRARY_PATH" not in env:
        env["DYLD_LIBRARY_PATH"] = os.path.abspath(default_sdk_path)

    print(f"[PUB] Publishing to channel '{channel}' — video from file, audio from TTS via stdin"
          f" (video_delay={video_delay}s)")
    abs_h264 = os.path.abspath(h264_file)
    cmd = ["go", "run", sender, AGORA_APP_ID, channel, abs_h264, "stdin"]
    if video_delay > 0:
        cmd.append(str(video_delay))
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=base_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,  # new process group for clean kill
    )
    return proc


def _wait_for_publisher_audio(proc, timeout=15, tag="PUB"):
    """
    Wait for Go publisher to connect and start reading audio from stdin.
    Returns time.time() when "audio publishing started" is detected.
    After this, the caller should start the STT pipeline (audio feed needs stdin ready).
    Remaining stdout is read via proc.stdout by the caller.
    """
    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    audio_ready_time = None
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=min(remaining, 0.5))
            if not events:
                if proc.poll() is not None:
                    print(f"  [{tag}] WARNING: Publisher exited (code {proc.returncode}) before audio ready")
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            if not text:
                continue
            print(f"  [{tag}] {text}")
            if "audio publishing started" in text:
                audio_ready_time = time.time()
                print(f"  [{tag}] Audio ready — publisher accepting stdin")
                break
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    if audio_ready_time is None:
        audio_ready_time = time.time()
        print(f"  [{tag}] WARNING: Audio ready signal not received within {timeout}s")

    return audio_ready_time


def _wait_for_video_start(proc, timeout=30, tag="PUB"):
    """
    Wait for Go publisher to finish video delay and start sending frames.
    Returns time.time() when "video delay complete" is detected.
    Called after _wait_for_publisher_audio, reads from proc.stdout.
    """
    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    video_start = None
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=min(remaining, 0.5))
            if not events:
                if proc.poll() is not None:
                    print(f"  [{tag}] WARNING: Publisher exited (code {proc.returncode}) before video start")
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            if not text:
                continue
            print(f"  [{tag}] {text}")
            if "video delay complete" in text:
                video_start = time.time()
                print(f"  [{tag}] Video started — video_start set")
                break
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    if video_start is None:
        video_start = time.time()
        print(f"  [{tag}] WARNING: Video start signal not received within {timeout}s")

    return video_start


def kill_publisher(proc):
    """Kill the publisher and all child processes (Go compiler spawns child)."""
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=5)
        print("[PUB] Publisher killed.")


# ─── Sportradar events file ─────────────────────────────────────────────

# Simple pass pattern: "to Player." or "Player to Player."
_PASS_RE = _re_module.compile(r'^(to [A-Z]|[A-Z][a-z]+ to [A-Z])\w*\.$')


def _is_simple_pass(message):
    """True for boring pass events like 'to Diks.' or 'Elvedi to Nicolas.'"""
    return bool(_PASS_RE.match(message.strip()))


def run_events_fallback(events_file, tts, lang, oai_client, last_stt_time,
                        stop_event, match_time_start, lang_file=None,
                        video_delay=VIDEO_DELAY_S, sr_prefetcher=None):
    """
    Replay events with parallel TTS prefetching.

    APPEND events → sr_prefetcher.schedule() (parallel TTS fetch, timed playback)
    INTERRUPT events → tts.speak(interrupt=True) + sr_prefetcher.cancel_all()

    Timing: Events fire at match time. Video is already delayed by the Go publisher.
    play_at = match_time_start + offset (video_start already includes the delay).
    """
    events = load_events_file(events_file)
    if not events:
        print(f"[SR] No events in {events_file}")
        return

    total = len(events)
    print(f"[SR] Loaded {total} events, video_delay={video_delay}s, "
          f"prefetcher={'yes' if sr_prefetcher else 'no'}")

    # Build a translate function that uses current lang + voice at call time
    def make_translate_fn():
        def translate(text):
            cur_lang = get_current_lang(lang_file, lang) if lang_file else lang
            vid = voice_for_lang(cur_lang)
            if cur_lang == "en":
                return (text, vid)
            return (translate_text(oai_client, text, cur_lang), vid)
        return translate

    # With prefetcher: feed all events into the rolling-window feeder.
    # The feeder drip-feeds events into the prefetch queue as they approach
    # (within PREFETCH_HORIZON_S). On language change, flush() resets
    # the feeder so upcoming events are re-translated.
    if sr_prefetcher:
        # Build event list for feeder: (text, play_at, translate_fn_factory)
        event_list = []
        for offset, priority, message in events:
            play_at = match_time_start[0] + offset
            event_list.append((message, play_at, make_translate_fn))
            mm, ss = offset // 60, offset % 60
            delay_to_play = play_at - time.time()
            tag = "INT" if priority == "INTERRUPT" else "EVT"
            print(f"  [{_ts(tts.video_start)}] [SR {mm:02d}:{ss:02d} {tag}] "
                  f"\"{message[:60]}\" (registered, play in {delay_to_play:.1f}s)")

        sr_prefetcher.set_events(event_list)

        # Wait for INTERRUPT events at their match times to clear STT queue
        for offset, priority, message in events:
            if stop_event.is_set():
                break
            if priority != "INTERRUPT":
                continue

            # Wait until this event's match time
            while not stop_event.is_set():
                match_elapsed = time.time() - match_time_start[0]
                if offset <= match_elapsed:
                    break
                time.sleep(0.1)

            if stop_event.is_set():
                break

            print(f"  [{_ts(tts.video_start)}] [SR {offset // 60:02d}:{offset % 60:02d} INT] "
                  f"Clearing STT for INTERRUPT")

            # Clear STT without killing SR playback (GOAL is in _sr_audio_buf)
            tts.clear_stt()
            # Cancel SR events that play before the INTERRUPT
            interrupt_play_at = match_time_start[0] + offset
            sr_prefetcher.cancel_before(interrupt_play_at)
            last_stt_time[0] = time.time()
    else:
        # No prefetcher — old sequential path
        for idx, (offset, priority, message) in enumerate(events):
            if stop_event.is_set():
                break

            is_interrupt = (priority == "INTERRUPT")

            while not stop_event.is_set():
                match_elapsed = time.time() - match_time_start[0]
                if offset <= match_elapsed:
                    break
                time.sleep(0.1)

            if stop_event.is_set():
                break
            play_at = match_time_start[0] + offset
            mm, ss = offset // 60, offset % 60
            delay_to_play = play_at - time.time()
            tag = "INT" if is_interrupt else "EVT"
            print(f"  [{_ts(tts.video_start)}] [SR {mm:02d}:{ss:02d} {tag}] "
                  f"\"{message[:60]}\" (play in {delay_to_play:.1f}s)")
            tts.speak(message, interrupt=is_interrupt, play_at=play_at,
                      translate_fn=make_translate_fn())
            last_stt_time[0] = time.time()

    print(f"[SR] Events replay finished.")


# ─── Pipeline (session-aware) ────────────────────────────────────────────

def run_pipeline_for_session(session, args, h264_file, oai_client):
    """Run one cycle of the publish pipeline for a specific session."""
    last_stt_time = [time.time()]
    pub_proc = None
    tts = None
    tag = f"SESSION {session.id[:8]}"

    try:
        if h264_file:
            pub_proc = start_publisher(h264_file, session.channel, video_delay=args.video_delay)
            pub_tag = f"{tag} PUB"

            # Phase 1: wait for audio ready (publisher accepts stdin)
            _wait_for_publisher_audio(pub_proc, timeout=15, tag=pub_tag)

            tts = TTSEngine(audio_pipe=pub_proc.stdin)
            if ControlHandler.atmosphere_pcm:
                tts.set_atmosphere(ControlHandler.atmosphere_pcm)
            if ControlHandler.original_pcm:
                tts.set_original_audio(ControlHandler.original_pcm)
            session.tts_engine = tts
            # Temporary video_start — will be updated when video actually starts
            tts.video_start = time.time() + args.video_delay
            tts.start()

            # Start STT pipeline NOW so it processes audio during the video delay.
            # By the time video starts, we have video_delay seconds of translations ready.
            stt_thread = None
            if args.audio:
                stt_thread = threading.Thread(
                    target=run_stt_pipeline,
                    args=(args.audio, tts, args.deepgram_key, args.lang,
                          oai_client, last_stt_time, session.stop_event),
                    kwargs={"lang_file": session.lang_file,
                            "video_delay": args.video_delay,
                            "max_stt_duration": args.max_stt_duration,
                            "get_current_lang": get_current_lang},
                    daemon=True,
                )
                stt_thread.start()
                print(f"[{tag}] STT pipeline started (processing during {args.video_delay}s video delay)")

            # Phase 2: wait for video delay to complete
            video_start = _wait_for_video_start(
                pub_proc, timeout=int(args.video_delay) + 15, tag=pub_tag)

            # Log remaining stdout/stderr in background threads
            def _log_pub(stream, label):
                for line in stream:
                    text = line.decode(errors='replace').rstrip()
                    if not text or 'PushVideoEncodedData' in text or 'SESS_CTRL' in text:
                        continue
                    print(f"  [{pub_tag} {label}] {text}")
            threading.Thread(target=_log_pub, args=(pub_proc.stdout, "out"), daemon=True).start()
            threading.Thread(target=_log_pub, args=(pub_proc.stderr, "err"), daemon=True).start()

            # Update video_start to actual time video frames start arriving
            tts.video_start = video_start
            print(f"[{tag}] video_start updated — viewer sees video now")
        else:
            devnull = open(os.devnull, "wb")
            tts = TTSEngine(audio_pipe=devnull)
            if ControlHandler.atmosphere_pcm:
                tts.set_atmosphere(ControlHandler.atmosphere_pcm)
            if ControlHandler.original_pcm:
                tts.set_original_audio(ControlHandler.original_pcm)
            session.tts_engine = tts
            tts.video_start = time.time()
            tts.start()
            stt_thread = None

        # pipeline_running already set True by /start handler (before thread spawn)

        # Match time anchored to video_start — events fire relative to this
        match_time_start = [tts.video_start - args.events_offset]

        sr_prefetcher = None
        sr_thread = None
        if args.events:
            sr_prefetcher = SRPrefetcher(
                tts_engine=tts, api_key=ELEVENLABS_API_KEY, model=ELEVENLABS_MODEL,
            )
            session.sr_prefetcher = sr_prefetcher
            sr_prefetcher.start()

            sr_thread = threading.Thread(
                target=run_events_fallback,
                args=(args.events, tts, args.lang, oai_client,
                      last_stt_time, session.stop_event, match_time_start),
                kwargs={"lang_file": session.lang_file,
                        "video_delay": args.video_delay,
                        "sr_prefetcher": sr_prefetcher},
            )
            sr_thread.start()
            print(f"[{tag}] SR prefetcher + events running (offset {args.events_offset}s)")

        if stt_thread:
            # STT already running — wait for it
            stt_thread.join()
            # STT finished but video is still playing (video_delay behind audio).
            # Wait for remaining TTS to drain + video_delay so viewer sees everything.
            drain_end = time.time() + args.video_delay
            print(f"[{tag}] STT done — waiting {args.video_delay}s for video to catch up")
            while time.time() < drain_end and not session.stop_event.is_set():
                time.sleep(0.5)
        elif args.audio:
            run_stt_pipeline(
                args.audio, tts, args.deepgram_key, args.lang,
                oai_client, last_stt_time, session.stop_event,
                lang_file=session.lang_file,
                video_delay=args.video_delay,
                max_stt_duration=args.max_stt_duration,
                get_current_lang=get_current_lang,
            )
        elif sr_thread:
            # Events-only mode: wait for events to finish, then drain
            sr_thread.join()
            print(f"[{tag}] Events finished — waiting for SR + TTS to drain")
            # Wait for SR prefetcher to finish playing all scheduled events
            while (tts.queue_size() > 0 or tts.is_speaking.is_set()
                   or tts._sr_audio_buf or tts._sr_playback_ready.is_set()
                   or (sr_prefetcher and (sr_prefetcher._ready_events
                       or not sr_prefetcher._prefetch_queue.empty()))):
                if session.stop_event.is_set():
                    break
                time.sleep(0.2)
            print(f"[{tag}] All drained — pipeline complete")
        else:
            while not session.stop_event.is_set():
                time.sleep(0.5)

    finally:
        print(f"[{tag}] Cleaning up pipeline...")
        session.pipeline_running = False
        session.tts_engine = None
        session.stop_event.set()
        if sr_prefetcher:
            sr_prefetcher.stop()
        if tts:
            tts.stop()
        kill_publisher(pub_proc)


def main():
    parser = argparse.ArgumentParser(
        description="Live match: STT → ElevenLabs TTS → Go publisher → Agora"
    )
    parser.add_argument("--audio", help="Commentary audio file (mp3/wav)")
    parser.add_argument("--video-h264", help="Pre-encoded H.264 file for video")
    parser.add_argument("--video", help="Match video file (mp4, will be converted)")
    parser.add_argument("--events", help="Sportradar events file for fallback")
    parser.add_argument("--lang", default="es", help="Output language (default: es)")
    parser.add_argument("--deepgram-key",
                        default=os.environ.get("DEEPGRAM_API_KEY", ""))
    parser.add_argument("--video-delay", type=float, default=VIDEO_DELAY_S,
                        help=f"Video delay in seconds (default: {VIDEO_DELAY_S})")
    parser.add_argument("--lang-port", type=int, default=8090,
                        help="Port for language control HTTP server (default: 8090)")
    parser.add_argument("--events-offset", type=int, default=0,
                        help="Match-time offset in seconds for events replay (default: 0)")
    parser.add_argument("--atmosphere", help="Atmosphere audio file (16kHz mono wav)")
    parser.add_argument("--max-stt-duration", type=float, default=5.0,
                        help="Force-split STT interims longer than this (default: 5.0s)")
    args = parser.parse_args()

    if not args.audio and not args.events:
        parser.error("Provide --audio (STT source) and/or --events (Sportradar fallback)")

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set")
        sys.exit(1)

    if not AGORA_APP_ID or not AGORA_APP_CERT:
        print("AGORA_APP_ID and AGORA_APP_CERT must be set for multi-session token generation")
        sys.exit(1)

    # Resolve H.264 video file
    h264_file = args.video_h264
    if args.video and not h264_file:
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "go-audio-video-publisher")
        encoded_dir = os.path.join(base_dir, "encoded_assets")
        os.makedirs(encoded_dir, exist_ok=True)
        h264_file = os.path.join(encoded_dir, "match_720p25.h264")
        print(f"[VIDEO] Converting {args.video} to H.264...")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-y", "-i", args.video, "-an",
            "-vf", "scale=1280:720,fps=25",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-profile:v", "high", "-level", "3.1",
            "-preset", "veryfast",
            "-x264-params", "keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1",
            "-b:v", "2800k", "-maxrate", "3200k", "-bufsize", "6400k",
            "-f", "h264", h264_file,
        ], capture_output=True)

    # Load atmosphere audio if provided
    atmosphere_pcm = None
    if args.atmosphere:
        atmosphere_pcm = load_atmosphere(args.atmosphere)

    # Load original commentary audio for pass-through mode
    original_pcm = None
    if args.audio:
        original_pcm_path = convert_to_pcm(args.audio)
        with wave.open(original_pcm_path, 'rb') as wf:
            original_pcm = wf.readframes(wf.getnframes())
        os.unlink(original_pcm_path)
        print(f"[ORIG] Loaded {len(original_pcm)/32000:.1f}s of original audio")

    oai_client = openai.OpenAI()
    session_mgr = SessionManager()
    lang_name = LANG_NAMES.get(args.lang, args.lang)

    start_control_server(args.lang_port, session_mgr, args, h264_file, oai_client,
                         atmosphere_pcm=atmosphere_pcm, original_pcm=original_pcm)

    print(f"\n{'=' * 70}")
    print(f"  LIVE MATCH — Multi-Session Server ({lang_name} default)")
    print(f"  STT audio: {args.audio or 'None'}")
    print(f"  Video: {h264_file or 'None (TTS audio only)'}")
    print(f"  SR fallback: {args.events or 'None'}")
    print(f"  Atmosphere: {args.atmosphere or 'None'}")
    print(f"  Events offset: {args.events_offset}s")
    print(f"  Video delay: {args.video_delay}s")
    print(f"  API: http://localhost:{args.lang_port}/api/session")
    print(f"{'=' * 70}\n")
    print("[MAIN] Waiting for viewers to create sessions...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  Shutting down...")
        print("  Done.")


if __name__ == "__main__":
    main()

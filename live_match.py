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
import asyncio
import base64
import collections
import json
import os
import queue
import re as _re_module
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import openai
import websockets

from tokens import AccessToken, ServiceRtc

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
VIDEO_DELAY_S = 7.0
MAX_LATENCY_S = 3.5
SILENCE_GAP_S = 2.0

# ElevenLabs
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "7fGUbxDMrefqPDjc4Anc")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")

# Sportradar
SPORTRADAR_API_KEY = os.environ.get("SPORTRADAR_API_KEY", "")

# Audio constants
SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_10MS = 320  # 16000 * 1 * 2 bytes * 0.01s

# ─── Keyword terms for Deepgram ──────────────────────────────────────────

TERMS_LIST = [
    "Borussia Monchengladbach", "Heidenheim", "Gladbach", "BMG", "FCH",
    "Bundesliga", "Borussia-Park", "Monchengladbach", "Fohlenelf",
    "Franck Honorat", "Jens Castrop", "Shuto Machino",
    "Nico Elvedi", "Moritz Nicolas", "Kevin Diks", "Philipp Sander",
    "Yannick Engelhardt", "Rocco Reitz", "Joe Scally", "Kevin Stoger",
    "Florian Neuhaus", "Haris Tabakovic", "Hugo Bolin", "Gio Reyna",
    "Tim Kleindienst",
    "Budu Zivzivadze", "Marnon Busch", "Patrick Mainka", "Niklas Dorsch",
    "Eren Dinkci", "Jonas Fohrenbach", "Mathias Honsak", "Hennes Behrens",
    "Frank Schmidt", "Bastian Dankert",
    "Honorat", "Castrop", "Machino", "Elvedi", "Nicolas",
    "Diks", "Sander", "Engelhardt", "Reitz", "Scally", "Stoger",
    "Neuhaus", "Tabakovic", "Bolin", "Reyna",
    "Zivzivadze", "Busch", "Mainka", "Dorsch", "Dinkci", "Fohrenbach",
    "Honsak", "Behrens", "Dankert",
    "St. Pauli", "Sankt Pauli", "Freiburg",
    "Bosnia", "Herzegovina",
    "relegation", "last-gasp", "matchdays",
]

# ─── Deterministic corrections ───────────────────────────────────────────

CORRECTIONS = [
    ("Honsakovic in the blue", "Heidenheim in the blue"),
    ("Zivadze in the blue", "Heidenheim in the blue"),
    ("Flankert all in white", "Gladbach all in white"),
    ("Flag back all in white", "Gladbach all in white"),
    ("Fanback all in white", "Gladbach all in white"),
    ("Flankert", "Gladbach"),
    ("Flag back", "Gladbach"),
    ("Fanback", "Gladbach"),
    ("Gundesliga", "Bundesliga"),
    ("Saks Paoli", "St. Pauli"),
    ("Saks Pauly", "St. Pauli"),
    ("Fallen Elf", "Fohlenelf"),
    ("Rock Blossom", "Rock Bottom"),
    ("laxed gasp winner", "last-gasp winner"),
    ("at laxed gasp", "a last-gasp"),
    ("last guest winner", "last-gasp winner"),
    ("relegated battle", "relegation battle"),
    ("in the lead.", "in the league."),
    ("in the lead,", "in the league,"),
    ("Not one a game", "Not won a game"),
    ("three hole draw", "three-all draw"),
    ("three o draw", "three-all draw"),
    ("15.27 games", "15 points from 27 games"),
    ("beat 5.21", "beat Freiburg 2-1"),
    ("Brightman rivals", "Rheinland Rivals"),
    ("Brightland rivals", "Rheinland Rivals"),
    ("at Brightman.", "at Rheinland Rivals, Koln."),
    ("Bolznier Herzegovina", "Bosnia-Herzegovina"),
    ("Bolznik Honsakovic", "Bosnia-Herzegovina"),
    ("heroic self pulse. Near Herzegovina", "heroics helping Bosnia-Herzegovina"),
    ("heroic self in Bosnia", "heroics helping Bosnia"),
    ("Ubijzivzivadze", "Budu Zivzivadze"),
    ("Mubu Zivzivadze", "Budu Zivzivadze"),
    ("Mubi Zivzivadze", "Budu Zivzivadze"),
    ("Bolt Bastian national GT in South Korea", "Bolin has been on international duty with South Korea"),
    ("Korea is fit for this one", "Bolin is fit for this one"),
    ("big six in for", "this season for"),
    ("Big six in for", "This season for"),
    ("Falled by", "Fouled by"),
    ("Fanged way back", "Banged away back"),
    ("Bright Shuto", "Shuto"),
    ("in a run.", "in a row."),
]


def apply_corrections(text):
    for wrong, right in CORRECTIONS:
        text = text.replace(wrong, right)
    return text


# ─── Translation ─────────────────────────────────────────────────────────

LANG_NAMES = {
    "es": "Spanish (Latin American)", "fr": "French", "de": "German",
    "pt": "Portuguese (Brazilian)", "it": "Italian", "ar": "Arabic",
    "ja": "Japanese", "ko": "Korean", "zh": "Mandarin Chinese", "hi": "Hindi",
    "en": "English",
}

# ElevenLabs voice IDs per language
LANG_VOICES = {
    "es": "jdSy6qWNc1T4C8czPgat",
    "de": "g8JjujAzgjLre020BW2u",
}
DEFAULT_VOICE_ID = "ImsA1Fn5TNc843fFdz99"


def voice_for_lang(lang):
    return LANG_VOICES.get(lang, DEFAULT_VOICE_ID)


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
                self._respond(200, {"lang": lang})
            except OSError as e:
                self._respond(500, {"error": str(e)})

        elif action == "status":
            lang = "es"
            try:
                with open(session.lang_file) as f:
                    lang = f.read().strip()
            except OSError:
                pass
            self._respond(200, {
                "running": session.pipeline_running,
                "lang": lang,
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


def start_control_server(port, session_mgr, args, h264_file, oai_client):
    """Start the control HTTP server in a daemon thread."""
    ControlHandler.session_mgr = session_mgr
    ControlHandler.args = args
    ControlHandler.h264_file = h264_file
    ControlHandler.oai_client = oai_client
    server = HTTPServer(("0.0.0.0", port), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[CTL] Control server on http://localhost:{port}")
    print(f"      POST /api/session  →  create session")
    print(f"      GET  /api/session/{{id}}/start|stop|set-lang|status")
    return server


TRANSLATE_SYSTEM = """You are a real-time translator for live soccer commentary.
Translate the English soccer commentary to {lang_name}. Rules:
1. Keep player names, team names, and proper nouns unchanged
2. Maintain the energy and rhythm of live commentary — this will be spoken aloud by TTS
3. Use natural soccer terminology for the target language
4. Return ONLY the translation, no explanations
5. Keep it concise — match the length of the original"""


def translate_text(oai_client, text, lang):
    lang_name = LANG_NAMES.get(lang, lang)
    resp = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM.format(lang_name=lang_name)},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        max_tokens=512,
    )
    return resp.choices[0].message.content.strip()


# ─── ElevenLabs TTS (WebSocket streaming) ────────────────────────────────

def _ts(video_start=None):
    """Current clock time for log stamps, with optional video-relative time."""
    wall = time.strftime("%H:%M:%S")
    if video_start is not None:
        vt = time.time() - video_start
        m, s = divmod(vt, 60)
        return f"{wall} V+{int(m):02d}:{s:05.2f}"
    return wall


class TTSEngine:
    """
    ElevenLabs WebSocket TTS → PCM → Go publisher stdin.

    Architecture:
      - TTS worker processes one utterance at a time from the text queue.
      - Each utterance is fully buffered from ElevenLabs before playback begins
        (pre-buffer) to avoid underruns from network jitter.
      - Pipe-writer drains the buffer at a steady 10ms rate to the Go publisher.
      - When buffer empties mid-playback, it logs an underrun.
      - No silence is ever sent — Go publisher handles silence on its own.
      - speak() queues text. Only real INTERRUPT events clear the queue.
    """

    def __init__(self, audio_pipe, voice_id=ELEVENLABS_VOICE_ID,
                 model=ELEVENLABS_MODEL, api_key=ELEVENLABS_API_KEY):
        self.audio_pipe = audio_pipe
        self.voice_id = voice_id
        self.model = model
        self.api_key = api_key
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        # Thread-safe audio buffer: TTS pushes bytes, pipe-writer drains
        self._audio_buf = collections.deque()
        self._buf_lock = threading.Lock()
        # Signals that a complete utterance is buffered and ready for playback
        self._playback_ready = threading.Event()
        # Text queue for non-blocking speak()
        self._text_queue = queue.Queue()
        self._loop = None
        # Callback when TTS finishes an utterance (called from worker thread)
        self.on_idle = None  # set externally: callable()
        # Track speaking state
        self.is_speaking = threading.Event()
        # SR (Sportradar) separate audio buffer — fed by SRPrefetcher
        self._sr_audio_buf = collections.deque()
        self._sr_buf_lock = threading.Lock()
        self._sr_playback_ready = threading.Event()
        # Wakes pipe writer for either STT or SR audio
        self._any_playback_ready = threading.Event()
        # When set, TTS worker discards current utterance output (SR GOAL playing)
        self._stt_suppressed = threading.Event()
        # Stats
        self._utterance_id = 0
        # Video-relative timestamp (set by pipeline after publisher starts)
        self.video_start = None

    def _vts(self):
        """Timestamp with video-relative time if available."""
        return _ts(self.video_start)

    def start(self):
        """Start pipe-writer and TTS worker threads."""
        threading.Thread(target=self._pipe_writer, daemon=True).start()
        threading.Thread(target=self._tts_worker, daemon=True).start()

    def _pipe_writer(self):
        """
        Drains audio buffers at 10ms rate. Checks both STT (_audio_buf) and
        SR (_sr_audio_buf). STT has priority: if STT audio becomes ready while
        SR is playing, SR is interrupted. SR never interrupts STT.
        """
        while not self._stop.is_set():
            # Block until either source has audio ready
            while not self._stop.is_set():
                if self._any_playback_ready.wait(timeout=0.5):
                    break
            if self._stop.is_set():
                break

            self._any_playback_ready.clear()

            # Determine source: STT has priority
            if self._playback_ready.is_set():
                source = "STT"
                self._playback_ready.clear()
                buf = self._audio_buf
                lock = self._buf_lock
            elif self._sr_playback_ready.is_set():
                source = "SR"
                self._sr_playback_ready.clear()
                buf = self._sr_audio_buf
                lock = self._sr_buf_lock
            else:
                continue

            with lock:
                n_chunks = len(buf)
            if n_chunks == 0:
                continue

            print(f"  [{self._vts()}] [PIPE] {source} playback started — {n_chunks * 10}ms buffered")
            next_tick = time.monotonic()

            while not self._stop.is_set() and not self._interrupt.is_set():
                # During SR playback, check if STT has become ready — interrupt SR
                if source == "SR" and self._playback_ready.is_set():
                    with self._sr_buf_lock:
                        self._sr_audio_buf.clear()
                    print(f"  [{self._vts()}] [PIPE] SR interrupted by STT")
                    # Don't clear _any_playback_ready — STT needs it
                    break

                chunk = None
                with lock:
                    if buf:
                        chunk = buf.popleft()

                if not chunk:
                    break  # utterance done

                try:
                    self.audio_pipe.write(chunk)
                    self.audio_pipe.flush()
                except (BrokenPipeError, OSError):
                    print(f"  [{self._vts()}] [PIPE] Pipe closed")
                    self._stop.set()
                    break

                next_tick += 0.01
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)

            print(f"  [{self._vts()}] [PIPE] {source} playback ended")

            # If SR was interrupted by STT, loop back — _any_playback_ready
            # is still set from the STT _playback_ready.set() call
            if source == "SR" and self._playback_ready.is_set():
                self._any_playback_ready.set()

    def _push_audio(self, pcm_bytes):
        """Split PCM bytes into 10ms chunks and push to buffer."""
        with self._buf_lock:
            if self._interrupt.is_set():
                return
            offset = 0
            while offset < len(pcm_bytes):
                end = offset + BYTES_PER_10MS
                chunk = pcm_bytes[offset:end]
                if len(chunk) < BYTES_PER_10MS:
                    chunk = chunk + b'\x00' * (BYTES_PER_10MS - len(chunk))
                self._audio_buf.append(chunk)
                offset = end

    def speak(self, text, interrupt=False, play_at=None, translate_fn=None):
        """
        Non-blocking. Queues text for sequential TTS playback.
        text: English text to speak (will be translated just-in-time if translate_fn set)
        play_at: absolute time.time() when playback should start (pre-fetch + hold)
        translate_fn: callable(text) -> translated_text (uses current lang at TTS time)
        """
        if interrupt:
            self._interrupt.set()
            self._stt_suppressed.clear()
            with self._buf_lock:
                self._audio_buf.clear()
            with self._sr_buf_lock:
                self._sr_audio_buf.clear()
            self._sr_playback_ready.clear()
            while not self._text_queue.empty():
                try:
                    self._text_queue.get_nowait()
                except queue.Empty:
                    break
            print(f"  [{self._vts()}] [TTS] Interrupted — STT+SR queues cleared")
        if text:
            if play_at and not interrupt:
                discarded = 0
                while not self._text_queue.empty():
                    try:
                        self._text_queue.get_nowait()
                        discarded += 1
                    except queue.Empty:
                        break
                if discarded:
                    print(f"  [{self._vts()}] [TTS] Replaced {discarded} stale queued item(s)")
            self._text_queue.put((text, play_at, translate_fn))

    def clear_stt(self):
        """
        Clear STT queue and audio without interrupting SR playback.
        Used when an SR INTERRUPT event (e.g. GOAL) is already playing
        in _sr_audio_buf and we want to prevent STT from preempting it.
        Sets _stt_suppressed so the TTS worker discards its in-flight
        utterance instead of signaling _playback_ready.
        """
        self._stt_suppressed.set()
        with self._buf_lock:
            self._audio_buf.clear()
        self._playback_ready.clear()
        while not self._text_queue.empty():
            try:
                self._text_queue.get_nowait()
            except queue.Empty:
                break
        print(f"  [{self._vts()}] [TTS] STT cleared (SR playback preserved)")

    def queue_size(self):
        return self._text_queue.qsize()

    def _tts_worker(self):
        """Processes TTS requests one at a time, sequentially."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while not self._stop.is_set():
            try:
                item = self._text_queue.get(timeout=0.1)
            except queue.Empty:
                if self.is_speaking.is_set():
                    self.is_speaking.clear()
                    print(f"  [{self._vts()}] [TTS] Queue empty — idle")
                    if self.on_idle:
                        self.on_idle()
                continue

            # Unpack — support both old (str) and new (tuple) format
            if isinstance(item, tuple):
                text, play_at, translate_fn = item
            else:
                text, play_at, translate_fn = item, None, None

            self._utterance_id += 1
            uid = self._utterance_id

            self.is_speaking.set()
            self._interrupt.clear()
            self._playback_ready.clear()

            # Just-in-time translation with current language + voice
            voice_id = self.voice_id  # default
            t_translate = time.monotonic()
            if translate_fn:
                try:
                    result = translate_fn(text)
                    if isinstance(result, tuple):
                        translated, voice_id = result
                    else:
                        translated = result
                except Exception:
                    translated = text
            else:
                translated = text
            translate_time = time.monotonic() - t_translate

            queued = self._text_queue.qsize()
            wc = len(translated.split())
            print(f"  [{self._vts()}] [TTS #{uid}] Starting — \"{translated[:50]}\" "
                  f"({wc}w, queue: {queued}, xlat: {translate_time:.2f}s, voice: {voice_id[:8]})")

            t0 = time.monotonic()
            self._loop.run_until_complete(self._tts(translated, uid, voice_id=voice_id))
            tts_time = time.monotonic() - t0

            if self._interrupt.is_set():
                print(f"  [{self._vts()}] [TTS #{uid}] Interrupted after {tts_time:.2f}s")
                with self._buf_lock:
                    self._audio_buf.clear()
                continue

            # SR GOAL is playing — discard this STT utterance
            if self._stt_suppressed.is_set():
                print(f"  [{self._vts()}] [TTS #{uid}] Suppressed (SR GOAL playing)")
                with self._buf_lock:
                    self._audio_buf.clear()
                self._stt_suppressed.clear()
                continue

            buf_chunks = len(self._audio_buf)
            buf_ms = buf_chunks * 10

            # Wait until scheduled play time if set
            if play_at:
                wait_s = play_at - time.time()
                if wait_s > 0:
                    print(f"  [{self._vts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — "
                          f"holding {wait_s:.2f}s for sync")
                    # Coarse sleep for the bulk of the wait
                    coarse = wait_s - 0.05
                    if coarse > 0:
                        time.sleep(coarse)
                    # Tight spin for the final ~50ms to hit ±1ms
                    while time.time() < play_at and not self._interrupt.is_set():
                        pass
                else:
                    late = -wait_s
                    # Drop if more than 100ms late — can't sync to original timing
                    if late > 0.1:
                        print(f"  [{self._vts()}] [TTS #{uid}] DROPPED {buf_ms}ms — {late:.2f}s past play_at")
                        with self._buf_lock:
                            self._audio_buf.clear()
                        continue
                    print(f"  [{self._vts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — "
                          f"playing now ({late:.2f}s late)")
            else:
                print(f"  [{self._vts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — starting playback")

            # Signal pipe writer that full utterance is ready
            self._playback_ready.set()
            self._any_playback_ready.set()

            # Wait for playback to drain
            drain_start = time.monotonic()
            while self._audio_buf and not self._interrupt.is_set():
                time.sleep(0.01)
            drain_time = time.monotonic() - drain_start

            print(f"  [{self._vts()}] [TTS #{uid}] Done — "
                  f"total: {tts_time + drain_time:.2f}s (tts: {tts_time:.2f}s + play: {drain_time:.2f}s)")

    async def _tts(self, text, uid, voice_id=None):
        """Connect to ElevenLabs WebSocket, send text, buffer all PCM.
        Retries once if no audio received (common with very short phrases)."""
        vid = voice_id or self.voice_id

        for attempt in range(2):
            send_text = text
            if attempt == 1:
                # Pad short text on retry — ElevenLabs sometimes fails on very short inputs
                send_text = text + "..."
                print(f"  [{self._vts()}] [TTS #{uid}] Retrying with padded text")

            chunk_count = await self._tts_once(send_text, uid, vid)
            if chunk_count > 0 or self._interrupt.is_set():
                break
            print(f"  [{self._vts()}] [TTS #{uid}] WARNING: No audio received from ElevenLabs"
                  f"{' (will retry)' if attempt == 0 else ''}")

    async def _tts_once(self, text, uid, voice_id):
        """Single ElevenLabs WebSocket TTS attempt. Returns chunk count."""
        uri = (f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
               f"/stream-input?model_id={self.model}&output_format=pcm_16000")

        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.8,
                    },
                    "xi_api_key": self.api_key,
                }))

                await ws.send(json.dumps({
                    "text": text,
                    "try_trigger_generation": True,
                }))

                await ws.send(json.dumps({"text": ""}))

                chunk_count = 0
                async for message in ws:
                    if self._interrupt.is_set():
                        break

                    data = json.loads(message)

                    if data.get("audio"):
                        pcm_bytes = base64.b64decode(data["audio"])
                        self._push_audio(pcm_bytes)
                        chunk_count += 1
                        if chunk_count == 1:
                            print(f"  [{self._vts()}] [TTS #{uid}] First audio chunk received")

                    if data.get("isFinal"):
                        break

                return chunk_count

        except Exception as e:
            print(f"  [{self._vts()}] [TTS #{uid}] ERROR: {e}")
            return 0

    def stop(self):
        self._stop.set()
        self._interrupt.set()


# ─── SR Prefetcher (parallel TTS fetch for Sportradar events) ─────────

class SRPrefetcher:
    """
    Fetches ElevenLabs TTS for Sportradar events in parallel with the STT
    pipeline, then injects audio into the TTSEngine's SR buffer at the
    scheduled play_at time.

    Architecture:
      _prefetch_worker: dequeues events, translates, fetches TTS → _ready_events
      _scheduler_worker: polls _ready_events, waits for play_at, injects into
                         tts._sr_audio_buf at the right moment
    """

    def __init__(self, tts_engine, api_key=ELEVENLABS_API_KEY,
                 model=ELEVENLABS_MODEL):
        self.tts = tts_engine
        self.api_key = api_key
        self.model = model
        self._stop = threading.Event()
        self._prefetch_queue = queue.Queue()
        self._ready_events = {}  # event_id → (pcm_bytes, play_at)
        self._ready_lock = threading.Lock()
        self._next_id = 0

    def schedule(self, text, play_at, translate_fn):
        """Schedule an SR event for prefetching and timed playback."""
        self._next_id += 1
        eid = self._next_id
        self._prefetch_queue.put((eid, text, play_at, translate_fn))
        return eid

    def cancel_all(self):
        """Clear all pending and ready events (called on INTERRUPT)."""
        while not self._prefetch_queue.empty():
            try:
                self._prefetch_queue.get_nowait()
            except queue.Empty:
                break
        with self._ready_lock:
            self._ready_events.clear()

    def cancel_except(self, keep_eid):
        """Cancel ready events that play before the INTERRUPT, keep future ones.

        The INTERRUPT event (keep_eid) and any events with later play_at are
        preserved so a GOAL doesn't permanently silence later commentary.
        The prefetch queue is not drained — future events still get fetched.
        """
        with self._ready_lock:
            if not keep_eid or keep_eid not in self._ready_events:
                return
            keep_play_at = self._ready_events[keep_eid][1]
            # Remove events that play at or before the INTERRUPT
            to_remove = [eid for eid, (_, play_at) in self._ready_events.items()
                         if eid != keep_eid and play_at <= keep_play_at]
            for eid in to_remove:
                del self._ready_events[eid]

    def start(self):
        """Spawn prefetch and scheduler worker threads."""
        threading.Thread(target=self._prefetch_worker, daemon=True).start()
        threading.Thread(target=self._scheduler_worker, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _vts(self):
        return _ts(self.tts.video_start)

    def _prefetch_worker(self):
        """Dequeue events, translate, fetch TTS audio, store in _ready_events."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self._stop.is_set():
            try:
                item = self._prefetch_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            eid, text, play_at, translate_fn = item

            # Translate
            voice_id = self.tts.voice_id
            if translate_fn:
                try:
                    result = translate_fn(text)
                    if isinstance(result, tuple):
                        translated, voice_id = result
                    else:
                        translated = result
                except Exception:
                    translated = text
            else:
                translated = text

            lead_time = play_at - time.time()
            print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Fetching — "
                  f"\"{translated[:50]}\" (play in {lead_time:.1f}s)")

            # Fetch TTS
            t0 = time.monotonic()
            pcm_bytes = loop.run_until_complete(
                self._fetch_tts(translated, voice_id, eid)
            )
            fetch_time = time.monotonic() - t0

            if pcm_bytes and len(pcm_bytes) > 0:
                with self._ready_lock:
                    self._ready_events[eid] = (pcm_bytes, play_at)
                lead = play_at - time.time()
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Ready — "
                      f"{len(pcm_bytes)}B in {fetch_time:.2f}s, "
                      f"{lead:.2f}s before play_at")
            else:
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] WARNING: No audio received")

        loop.close()

    async def _fetch_tts(self, text, voice_id, eid):
        """
        Fetch TTS from ElevenLabs WebSocket. Same protocol as TTSEngine._tts_once
        but returns concatenated PCM bytes instead of pushing to shared buffer.
        Retries once with padded text on zero audio.
        """
        for attempt in range(2):
            send_text = text
            if attempt == 1:
                send_text = text + "..."
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Retrying with padded text")

            pcm_parts = []
            uri = (f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
                   f"/stream-input?model_id={self.model}&output_format=pcm_16000")
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({
                        "text": " ",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.8,
                        },
                        "xi_api_key": self.api_key,
                    }))
                    await ws.send(json.dumps({
                        "text": send_text,
                        "try_trigger_generation": True,
                    }))
                    await ws.send(json.dumps({"text": ""}))

                    async for message in ws:
                        if self._stop.is_set():
                            return b""
                        data = json.loads(message)
                        if data.get("audio"):
                            pcm_parts.append(base64.b64decode(data["audio"]))
                        if data.get("isFinal"):
                            break

                result = b"".join(pcm_parts)
                if len(result) > 0:
                    return result
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] WARNING: No audio"
                      f"{' (will retry)' if attempt == 0 else ''}")

            except Exception as e:
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] ERROR: {e}")
                if attempt == 0:
                    continue
                return b""

        return b""

    def _scheduler_worker(self):
        """
        Polls _ready_events for events whose play_at is approaching.
        Uses two-phase wait: coarse sleep + tight spin for ±1ms precision.
        Injects PCM chunks into tts._sr_audio_buf at the right moment.
        """
        while not self._stop.is_set():
            # Find the next event to play
            now = time.time()
            next_eid = None
            next_play_at = None

            with self._ready_lock:
                for eid, (pcm_bytes, play_at) in self._ready_events.items():
                    if next_play_at is None or play_at < next_play_at:
                        next_eid = eid
                        next_play_at = play_at

            if next_eid is None:
                time.sleep(0.01)
                continue

            now = time.time()
            wait = next_play_at - now

            # Not yet time — sleep and re-check
            if wait > 0.1:
                time.sleep(min(wait - 0.05, 0.05))
                continue

            # Close to play_at — extract the event
            with self._ready_lock:
                entry = self._ready_events.pop(next_eid, None)
            if entry is None:
                continue

            pcm_bytes, play_at = entry

            # Coarse sleep for bulk of remaining wait
            remaining = play_at - time.time()
            if remaining > 0.05:
                time.sleep(remaining - 0.05)

            # Tight spin for final ~50ms
            while time.time() < play_at and not self._stop.is_set():
                pass

            delta_ms = (time.time() - play_at) * 1000
            dur_ms = len(pcm_bytes) / (SAMPLE_RATE * 2) * 1000

            # Split into 10ms chunks and inject into SR buffer
            with self.tts._sr_buf_lock:
                offset = 0
                while offset < len(pcm_bytes):
                    end = offset + BYTES_PER_10MS
                    chunk = pcm_bytes[offset:end]
                    if len(chunk) < BYTES_PER_10MS:
                        chunk = chunk + b'\x00' * (BYTES_PER_10MS - len(chunk))
                    self.tts._sr_audio_buf.append(chunk)
                    offset = end

            # Signal pipe writer
            self.tts._sr_playback_ready.set()
            self.tts._any_playback_ready.set()

            print(f"  [{self._vts()}] [SR SCHED #{next_eid}] Injected — "
                  f"{dur_ms:.0f}ms, delta {delta_ms:+.0f}ms")


# ─── Audio helpers ───────────────────────────────────────────────────────

def convert_to_pcm(audio_path):
    import tempfile
    pcm_path = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path,
         "-ar", "16000", "-ac", "1", "-f", "wav", pcm_path],
        capture_output=True,
    )
    return pcm_path


def pcm_chunks_realtime(wav_path, chunk_ms=100):
    bytes_per_sec = 32000
    chunk_bytes = int(bytes_per_sec * chunk_ms / 1000)
    chunk_duration = chunk_ms / 1000.0
    with open(wav_path, "rb") as f:
        f.read(44)  # skip WAV header
        audio_offset = 0.0
        t_start = time.monotonic()
        while True:
            data = f.read(chunk_bytes)
            if not data:
                break
            yield data, audio_offset
            audio_offset += chunk_duration
            target = t_start + audio_offset
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)


# ─── Video + Audio publisher ────────────────────────────────────────────

def start_publisher(h264_file, channel, video_delay=0):
    """
    Launch Go publisher that reads H.264 video from file and
    PCM audio from stdin. video_delay seconds before sending video frames.
    """
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "go-audio-video-publisher")
    sender = os.path.join(base_dir, "reference", "agora_go_sdk", "send_h264_pcm_uid73.go")
    sdk_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "codex",
        "server-custom-llm", "go-audio-subscriber", "sdk", "agora_sdk_mac"
    )

    env = os.environ.copy()
    env["AGORA_APP_CERTIFICATE"] = AGORA_APP_CERT
    env["DYLD_LIBRARY_PATH"] = os.path.abspath(sdk_path)

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

def load_events_file(filepath):
    events = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            ts = parts[0]
            if ':' in ts:
                mm, ss = ts.split(':')
                offset = int(mm) * 60 + int(ss)
            else:
                offset = int(ts)
            events.append((offset, parts[1], parts[2]))
    return events


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

    # With prefetcher: schedule ALL events upfront for maximum prefetch
    # lead time. INTERRUPT events are also pre-fetched but at match time
    # they clear everything else so they play on time.
    if sr_prefetcher:
        # Track INTERRUPT event IDs so we can protect them from cancel_all
        interrupt_eids = {}  # offset → event_id

        # Schedule all events upfront
        for idx, (offset, priority, message) in enumerate(events):
            play_at = match_time_start[0] + offset
            mm, ss = offset // 60, offset % 60
            delay_to_play = play_at - time.time()
            tag = "INT" if priority == "INTERRUPT" else "EVT"
            print(f"  [{_ts(tts.video_start)}] [SR {mm:02d}:{ss:02d} {tag}] "
                  f"\"{message[:60]}\" (scheduled, play in {delay_to_play:.1f}s)")
            eid = sr_prefetcher.schedule(message, play_at, make_translate_fn())
            if priority == "INTERRUPT":
                interrupt_eids[offset] = eid

        # Wait for INTERRUPT events at their match times to clear STT queue
        for idx, (offset, priority, message) in enumerate(events):
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
            # Cancel other pending SR events but keep the INTERRUPT event
            sr_prefetcher.cancel_except(interrupt_eids.get(offset))
            last_stt_time[0] = time.time()
    else:
        # No prefetcher — old sequential path
        for idx, (offset, priority, message) in enumerate(events):
            if stop_event.is_set():
                break

            while not stop_event.is_set():
                match_elapsed = time.time() - match_time_start[0]
                if offset <= match_elapsed:
                    break
                time.sleep(0.1)

            if stop_event.is_set():
                break

            is_interrupt = (priority == "INTERRUPT")
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


# ─── STT pipeline ────────────────────────────────────────────────────────

def run_stt_pipeline(audio_path, tts, deepgram_key, lang, oai_client,
                     last_stt_time, stop_event, lang_file=None,
                     video_delay=3.0):
    """
    Stream audio through Deepgram → Corrections → Translate → ElevenLabs TTS.
    Uses play_at scheduling: each utterance plays at video_start + audio_start.
    Video is already delayed by video_delay (Go publisher), so video_start
    includes the delay and play_at needs no extra offset.
    """
    os.environ["DEEPGRAM_API_KEY"] = deepgram_key
    from deepgram import DeepgramClient
    from deepgram.listen import ListenV1Results

    pcm_path = convert_to_pcm(audio_path)
    dg_client = DeepgramClient()

    print(f"[STT] Streaming {audio_path} through Deepgram Nova-3...")
    print(f"[STT] Pipeline: STT → Correct → Translate({lang}) → ElevenLabs TTS → Agora")
    print(f"[STT] Video delay: {video_delay}s (pipeline budget)\n")

    wall_start = [None]

    with dg_client.listen.v1.connect(
        model="nova-3",
        language="en",
        encoding="linear16",
        sample_rate=16000,
        punctuate="true",
        smart_format="true",
        interim_results="true",
        utterance_end_ms="1000",
        endpointing="200",
        keyterm=TERMS_LIST,
    ) as ws:

        def feed_audio():
            for chunk, _ in pcm_chunks_realtime(pcm_path):
                ws.send_media(chunk)
            ws.send_close_stream()

        wall_start[0] = time.time()
        audio_feed_offset = wall_start[0] - tts.video_start
        print(f"[STT] Audio feed starting — {audio_feed_offset:.2f}s after video_start")
        audio_thread = threading.Thread(target=feed_audio, daemon=True)
        audio_thread.start()

        for msg in ws:
            if stop_event.is_set():
                break
            if not isinstance(msg, ListenV1Results):
                continue
            if not msg.is_final:
                continue

            alt = msg.channel.alternatives[0]
            transcript = alt.transcript
            if not transcript:
                continue

            audio_start = msg.start if hasattr(msg, "start") and msg.start else 0
            audio_end = audio_start + (msg.duration if hasattr(msg, "duration") and msg.duration else 0)

            corrected = apply_corrections(transcript)

            # play_at = when the viewer sees this moment
            # video_start is already delayed by video_delay (Go publisher sleeps first)
            # so play_at = video_start + audio_start (no extra delay offset)
            play_at = tts.video_start + audio_start
            remaining = play_at - time.time()

            print(f"  [{_ts(tts.video_start)}] [STT] audio={audio_start:.1f}-{audio_end:.1f}s "
                  f"remaining={remaining:.2f}s play_at=V+{audio_start:.1f}")
            print(f"           \"{corrected[:70]}\"")

            # JIT translation — translate at TTS time so language switches
            # take effect immediately, not when STT result was received
            def make_stt_translate_fn():
                def translate(text):
                    cur_lang = get_current_lang(lang_file, lang) if lang_file else lang
                    vid = voice_for_lang(cur_lang)
                    if cur_lang == "en":
                        return (text, vid)
                    return (translate_text(oai_client, text, cur_lang), vid)
                return translate

            tts.speak(corrected, play_at=play_at, translate_fn=make_stt_translate_fn())
            last_stt_time[0] = time.time()

    os.unlink(pcm_path)
    print("[STT] Pipeline finished.")


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
                            "video_delay": args.video_delay},
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

    oai_client = openai.OpenAI()
    session_mgr = SessionManager()
    lang_name = LANG_NAMES.get(args.lang, args.lang)

    start_control_server(args.lang_port, session_mgr, args, h264_file, oai_client)

    print(f"\n{'=' * 70}")
    print(f"  LIVE MATCH — Multi-Session Server ({lang_name} default)")
    print(f"  STT audio: {args.audio or 'None'}")
    print(f"  Video: {h264_file or 'None (TTS audio only)'}")
    print(f"  SR fallback: {args.events or 'None'}")
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

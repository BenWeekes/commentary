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
VIDEO_DELAY_S = 3.0
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
    _SESSION_RE = _re_module.compile(r'^/api/session/([a-f0-9]+)/(\w+)$')

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

def _ts():
    """Current clock time for log stamps."""
    return time.strftime("%H:%M:%S")


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
        # Stats
        self._utterance_id = 0

    def start(self):
        """Start pipe-writer and TTS worker threads."""
        threading.Thread(target=self._pipe_writer, daemon=True).start()
        threading.Thread(target=self._tts_worker, daemon=True).start()

    def _pipe_writer(self):
        """
        Drains audio buffer at 10ms rate. Waits for _playback_ready before
        starting each utterance — full pre-buffer eliminates underruns.
        """
        while not self._stop.is_set():
            # Wait until a complete utterance is buffered
            self._playback_ready.wait(timeout=0.1)
            if self._stop.is_set():
                break
            if not self._playback_ready.is_set():
                continue

            self._playback_ready.clear()

            with self._buf_lock:
                n_chunks = len(self._audio_buf)
            if n_chunks == 0:
                continue

            print(f"  [{_ts()}] [PIPE] Playback started — {n_chunks * 10}ms buffered")
            next_tick = time.monotonic()

            while not self._stop.is_set() and not self._interrupt.is_set():
                chunk = None
                with self._buf_lock:
                    if self._audio_buf:
                        chunk = self._audio_buf.popleft()

                if not chunk:
                    break  # utterance done

                try:
                    self.audio_pipe.write(chunk)
                    self.audio_pipe.flush()
                except (BrokenPipeError, OSError):
                    print(f"  [{_ts()}] [PIPE] Pipe closed")
                    self._stop.set()
                    break

                next_tick += 0.01
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)

            print(f"  [{_ts()}] [PIPE] Playback ended")

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
            with self._buf_lock:
                self._audio_buf.clear()
            while not self._text_queue.empty():
                try:
                    self._text_queue.get_nowait()
                except queue.Empty:
                    break
            print(f"  [{_ts()}] [TTS] Interrupted — queue cleared")
        self._text_queue.put((text, play_at, translate_fn))

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
                    print(f"  [{_ts()}] [TTS] Queue empty — idle")
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

            queued = self._text_queue.qsize()
            print(f"  [{_ts()}] [TTS #{uid}] Starting — \"{translated[:50]}\" "
                  f"(queue: {queued}, voice: {voice_id[:8]})")

            t0 = time.monotonic()
            self._loop.run_until_complete(self._tts(translated, uid, voice_id=voice_id))
            tts_time = time.monotonic() - t0

            if self._interrupt.is_set():
                print(f"  [{_ts()}] [TTS #{uid}] Interrupted after {tts_time:.2f}s")
                with self._buf_lock:
                    self._audio_buf.clear()
                continue

            buf_chunks = len(self._audio_buf)
            buf_ms = buf_chunks * 10

            # Wait until scheduled play time if set
            if play_at:
                wait_s = play_at - time.time()
                if wait_s > 0:
                    print(f"  [{_ts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — "
                          f"holding {wait_s:.2f}s for sync")
                    while time.time() < play_at and not self._interrupt.is_set():
                        time.sleep(0.01)
                else:
                    late = -wait_s
                    print(f"  [{_ts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — "
                          f"playing now ({late:.2f}s late)")
            else:
                print(f"  [{_ts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — starting playback")

            # Signal pipe writer that full utterance is ready
            self._playback_ready.set()

            # Wait for playback to drain
            drain_start = time.monotonic()
            while self._audio_buf and not self._interrupt.is_set():
                time.sleep(0.01)
            drain_time = time.monotonic() - drain_start

            print(f"  [{_ts()}] [TTS #{uid}] Done — "
                  f"total: {tts_time + drain_time:.2f}s (tts: {tts_time:.2f}s + play: {drain_time:.2f}s)")

    async def _tts(self, text, uid, voice_id=None):
        """Connect to ElevenLabs WebSocket, send text, buffer all PCM."""
        vid = voice_id or self.voice_id
        uri = (f"wss://api.elevenlabs.io/v1/text-to-speech/{vid}"
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
                first_chunk_time = None
                async for message in ws:
                    if self._interrupt.is_set():
                        break

                    data = json.loads(message)

                    if data.get("audio"):
                        pcm_bytes = base64.b64decode(data["audio"])
                        self._push_audio(pcm_bytes)
                        chunk_count += 1
                        if first_chunk_time is None:
                            first_chunk_time = time.monotonic()
                            print(f"  [{_ts()}] [TTS #{uid}] First audio chunk received")

                    if data.get("isFinal"):
                        break

                if chunk_count == 0:
                    print(f"  [{_ts()}] [TTS #{uid}] WARNING: No audio received from ElevenLabs")

        except Exception as e:
            print(f"  [{_ts()}] [TTS #{uid}] ERROR: {e}")

    def stop(self):
        self._stop.set()
        self._interrupt.set()


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

def start_publisher(h264_file, channel, delay_s=3.0):
    """
    Launch Go publisher that reads H.264 video from file and
    PCM audio from stdin. Returns (subprocess, stdin_pipe).
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

    print(f"[PUB] Waiting {delay_s}s before publishing (video delay)...")
    time.sleep(delay_s)

    print(f"[PUB] Publishing to channel '{channel}' — video from file, audio from TTS via stdin")
    abs_h264 = os.path.abspath(h264_file)
    proc = subprocess.Popen(
        ["go", "run", sender, AGORA_APP_ID, channel, abs_h264, "stdin"],
        env=env,
        cwd=base_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,  # new process group for clean kill
    )
    return proc


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
                        video_delay=VIDEO_DELAY_S):
    """
    Replay events sequentially — one TTS at a time, no interrupts.

    Timing: Events fire at match time. Video is delayed by video_delay seconds.
    So we pre-fetch TTS when match time arrives, then schedule playback for
    match_time + video_delay so audio syncs with delayed video.

    Filtering: Simple passes ("to Player." / "Player to Player.") are mostly
    skipped — only ~1 in 5 are kept to maintain a sense of play-by-play
    without overwhelming the listener. All INTERRUPT events are always kept.

    Translation is just-in-time (at TTS time, not queue time) so language
    changes take effect on the next utterance.
    """
    events = load_events_file(events_file)
    if not events:
        print(f"[SR] No events in {events_file}")
        return

    # Count types
    total = len(events)
    passes = sum(1 for _, _, m in events if _is_simple_pass(m))
    print(f"[SR] Loaded {total} events ({passes} passes, "
          f"{total - passes} interesting), video_delay={video_delay}s")

    # Build a translate function that uses current lang + voice at call time
    def make_translate_fn():
        def translate(text):
            cur_lang = get_current_lang(lang_file, lang) if lang_file else lang
            vid = voice_for_lang(cur_lang)
            if cur_lang == "en":
                return (text, vid)
            return (translate_text(oai_client, text, cur_lang), vid)
        return translate

    pass_count = 0
    skipped = 0

    for idx, (offset, priority, message) in enumerate(events):
        if stop_event.is_set():
            break

        # Filter simple passes — keep ~1 in 5
        if priority == "APPEND" and _is_simple_pass(message):
            pass_count += 1
            if pass_count % 5 != 0:
                skipped += 1
                continue

        # Wait until this event's match time arrives
        while not stop_event.is_set():
            match_elapsed = time.time() - match_time_start[0]
            if offset <= match_elapsed:
                break
            time.sleep(0.1)

        if stop_event.is_set():
            break

        # Don't queue more than 1 ahead — keeps language switching responsive
        while tts.queue_size() >= 1 and not stop_event.is_set():
            time.sleep(0.3)

        # Schedule playback for when video shows this moment
        play_at = match_time_start[0] + offset + video_delay

        mm = offset // 60
        ss = offset % 60
        delay_to_play = play_at - time.time()
        tag = "INT" if priority == "INTERRUPT" else "EVT"
        print(f"  [{_ts()}] [SR {mm:02d}:{ss:02d} {tag}] \"{message[:60]}\" "
              f"(play in {delay_to_play:.1f}s)")

        tts.speak(message, interrupt=False, play_at=play_at,
                  translate_fn=make_translate_fn())
        last_stt_time[0] = time.time()

    print(f"[SR] Events replay finished. Skipped {skipped} simple passes.")


# ─── STT pipeline ────────────────────────────────────────────────────────

def run_stt_pipeline(audio_path, tts, deepgram_key, lang, oai_client,
                     last_stt_time, stop_event, lang_file=None):
    """
    Stream audio through Deepgram → Corrections → Translate → ElevenLabs TTS.
    """
    os.environ["DEEPGRAM_API_KEY"] = deepgram_key
    from deepgram import DeepgramClient
    from deepgram.listen import ListenV1Results

    pcm_path = convert_to_pcm(audio_path)
    dg_client = DeepgramClient()

    print(f"[STT] Streaming {audio_path} through Deepgram Nova-3...")
    print(f"[STT] Pipeline: STT → Correct → Translate({lang}) → ElevenLabs TTS → Agora")
    print(f"[STT] Max latency budget: {MAX_LATENCY_S}s\n")

    wall_start = [None]

    with dg_client.listen.v1.connect(
        model="nova-3",
        language="en",
        encoding="linear16",
        sample_rate=16000,
        punctuate="true",
        smart_format="true",
        interim_results="true",
        keyterm=TERMS_LIST,
    ) as ws:

        def feed_audio():
            for chunk, _ in pcm_chunks_realtime(pcm_path):
                ws.send_media(chunk)
            ws.send_close_stream()

        wall_start[0] = time.time()
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

            wall_now = time.time() - wall_start[0]
            audio_start = msg.start if hasattr(msg, "start") and msg.start else 0
            audio_end = audio_start + (msg.duration if hasattr(msg, "duration") and msg.duration else 0)

            corrected = apply_corrections(transcript)

            total_latency = (time.time() - wall_start[0]) - audio_end

            if total_latency > MAX_LATENCY_S:
                print(f"  [DROP {total_latency:.1f}s] {corrected[:40]}")
                continue

            print(f"  [{audio_start:6.1f}s] lat={total_latency:.2f}s "
                  f"stt={wall_now - audio_end:.2f}s")
            print(f"           {corrected[:60]}")

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

            tts.speak(corrected, translate_fn=make_stt_translate_fn())
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
            pub_proc = start_publisher(h264_file, session.channel, delay_s=args.video_delay)
            def _log_pub(stream, label):
                for line in stream:
                    text = line.decode(errors='replace').rstrip()
                    if not text or 'PushVideoEncodedData' in text or 'SESS_CTRL' in text:
                        continue
                    print(f"  [{tag} PUB {label}] {text}")
            threading.Thread(target=_log_pub, args=(pub_proc.stdout, "out"), daemon=True).start()
            threading.Thread(target=_log_pub, args=(pub_proc.stderr, "err"), daemon=True).start()
            tts = TTSEngine(audio_pipe=pub_proc.stdin)
        else:
            devnull = open(os.devnull, "wb")
            tts = TTSEngine(audio_pipe=devnull)

        tts.start()
        session.pipeline_running = True

        # Match time starts now — offset by events-offset
        match_time_start = [time.time() - args.events_offset]

        if args.events:
            sr_thread = threading.Thread(
                target=run_events_fallback,
                args=(args.events, tts, args.lang, oai_client,
                      last_stt_time, session.stop_event, match_time_start),
                kwargs={"lang_file": session.lang_file,
                        "video_delay": args.video_delay},
                daemon=True,
            )
            sr_thread.start()
            print(f"[{tag}] Events fallback running (offset {args.events_offset}s)")

        if args.audio:
            run_stt_pipeline(
                args.audio, tts, args.deepgram_key, args.lang,
                oai_client, last_stt_time, session.stop_event,
                lang_file=session.lang_file,
            )
        else:
            while not session.stop_event.is_set():
                time.sleep(0.5)

    finally:
        print(f"[{tag}] Cleaning up pipeline...")
        session.pipeline_running = False
        session.stop_event.set()
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

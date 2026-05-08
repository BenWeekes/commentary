"""HTTP server for production match control and viewer token generation."""

import json
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

from server.token_api import generate_viewer_token

# Incrementing UID counter — each viewer request gets a unique UID.
# Publisher uses UID 73, so viewer UIDs start at 100 to avoid collision.
_uid_counter = 100
_uid_lock = threading.Lock()


def _next_uid():
    global _uid_counter
    with _uid_lock:
        uid = _uid_counter
        _uid_counter += 1
    return uid


class StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler for match status, token generation, and static files."""

    orchestrator = None  # set before server starts
    server_config = None

    _MATCH_STATUS_RE = re.compile(r'^/api/matches/([^/]+)/status$')
    _MATCH_ACTION_RE = re.compile(r'^/api/matches/([^/]+)/(start|stop)$')
    _MATCH_CHANNELS_RE = re.compile(r'^/api/matches/([^/]+)/channels$')
    _MATCH_TRANSCRIPT_RE = re.compile(r'^/api/matches/([^/]+)/transcript$')

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type="text/html; charset=utf-8"):
        """Serve a static file from the project root."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        filepath = os.path.join(root, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._respond(404, {"error": f"{filename} not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Static files
        if path in ("/", "/control.html"):
            self._serve_file("control.html")
            return
        if path == "/viewer_live.html":
            self._serve_file("viewer_live.html")
            return
        if path == "/status.html":
            self._serve_file("status.html")
            return

        # All match statuses
        if path == "/api/matches":
            statuses = self.orchestrator.get_all_status()
            # Build a config lookup for configured_languages
            cfg_langs = {}
            for mc in self.server_config.matches:
                cfg_langs[mc.match_id] = mc.languages
            result = {}
            for mid, s in statuses.items():
                result[mid] = {
                    "match_id": s.match_id,
                    "state": s.state,
                    "stt_utterance_count": s.stt_utterance_count,
                    "languages": s.languages,
                    "configured_languages": cfg_langs.get(mid, []),
                    "error": s.error,
                    "started_at": s.started_at,
                }
            self._respond(200, result)
            return

        # Single match status
        m = self._MATCH_STATUS_RE.match(path)
        if m:
            match_id = m.group(1)
            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return
            s = worker.status
            self._respond(200, {
                "match_id": s.match_id,
                "state": s.state,
                "stt_utterance_count": s.stt_utterance_count,
                "languages": s.languages,
                "error": s.error,
                "started_at": s.started_at,
            })
            return

        # Viewer channel set: returns tokens + UIDs for all languages in a match.
        # Each call allocates unique UIDs so multiple viewers don't collide.
        m = self._MATCH_CHANNELS_RE.match(path)
        if m:
            match_id = m.group(1)
            match_cfg = None
            for mc in self.server_config.matches:
                if mc.match_id == match_id:
                    match_cfg = mc
                    break
            if not match_cfg:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            cfg = self.server_config
            channels = {}
            for lang in match_cfg.languages:
                channel = f"{match_id}-{lang}"
                uid = _next_uid()
                token = generate_viewer_token(
                    cfg.agora_app_id, cfg.agora_app_cert,
                    channel, uid)
                channels[lang] = {
                    "channel": channel,
                    "token": token,
                    "uid": uid,
                }

            self._respond(200, {
                "match_id": match_id,
                "appid": cfg.agora_app_id,
                "channels": channels,
            })
            return

        # Recent English transcript for a match
        m = self._MATCH_TRANSCRIPT_RE.match(path)
        if m:
            match_id = m.group(1)
            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return
            self._respond(200, {
                "match_id": match_id,
                "transcript": worker.recent_transcript,
            })
            return

        self._respond(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/token":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid JSON"})
                return

            match_id = data.get("match_id", "")
            lang = data.get("lang", "")

            if not match_id or not lang:
                self._respond(400, {"error": "match_id and lang required"})
                return

            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            channel = f"{match_id}-{lang}"
            uid = _next_uid()
            cfg = self.server_config

            token = generate_viewer_token(
                cfg.agora_app_id, cfg.agora_app_cert,
                channel, uid)

            self._respond(200, {
                "token": token,
                "channel": channel,
                "uid": uid,
                "appid": cfg.agora_app_id,
            })
            return

        # Start/stop a match
        m = self._MATCH_ACTION_RE.match(path)
        if m:
            match_id, action = m.group(1), m.group(2)
            try:
                if action == "start":
                    self.orchestrator.start_match(match_id)
                else:
                    self.orchestrator.stop_match(match_id)
            except KeyError:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            worker = self.orchestrator.get_worker(match_id)
            s = worker.status
            self._respond(200, {
                "match_id": s.match_id,
                "state": s.state,
                "stt_utterance_count": s.stt_utterance_count,
                "languages": s.languages,
                "error": s.error,
                "started_at": s.started_at,
            })
            return

        self._respond(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_status_server(port, orchestrator, server_config):
    """Start the HTTP status server in a daemon thread."""
    StatusHandler.orchestrator = orchestrator
    StatusHandler.server_config = server_config
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[HTTP] Status server on http://localhost:{port}")
    print(f"       GET  /api/matches                  → all match statuses")
    print(f"       GET  /api/matches/{{id}}/status      → one match status")
    print(f"       GET  /api/matches/{{id}}/channels    → viewer tokens for all langs")
    print(f"       GET  /api/matches/{{id}}/transcript  → recent English transcript")
    print(f"       POST /api/matches/{{id}}/start       → start a match")
    print(f"       POST /api/matches/{{id}}/stop        → stop a match")
    print(f"       POST /api/token                    → viewer token (single)")
    print(f"       GET  /                             → control.html")
    print(f"       GET  /status.html                  → public status page")
    print(f"       GET  /viewer_live.html             → production viewer")
    return server

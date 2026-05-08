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
    _MATCH_LOGS_RE = re.compile(r'^/api/matches/([^/]+)/logs/([a-z]{2}|stt)$')
    _MATCH_DETAIL_RE = re.compile(r'^/api/matches/([^/]+)/detail$')
    _MATCH_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "match_data")

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
        if path == "/match_detail.html":
            self._serve_file("match_detail.html")
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

            # Add original audio channel
            if match_cfg.mode == "live" and match_cfg.source_channel:
                # Live: viewer joins the source channel directly
                orig_uid = _next_uid()
                orig_token = generate_viewer_token(
                    cfg.agora_app_id, cfg.agora_app_cert,
                    match_cfg.source_channel, orig_uid)
                channels["original"] = {
                    "channel": match_cfg.source_channel,
                    "token": orig_token,
                    "uid": orig_uid,
                }
            else:
                # Demo: dedicated original pipeline publishes on {match_id}-original
                orig_channel = f"{match_id}-original"
                orig_uid = _next_uid()
                orig_token = generate_viewer_token(
                    cfg.agora_app_id, cfg.agora_app_cert,
                    orig_channel, orig_uid)
                channels["original"] = {
                    "channel": orig_channel,
                    "token": orig_token,
                    "uid": orig_uid,
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

        # Match detail: keyterms, log dir, config
        m = self._MATCH_DETAIL_RE.match(path)
        if m:
            match_id = m.group(1)
            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            # Find match config
            match_cfg = None
            for mc in self.server_config.matches:
                if mc.match_id == match_id:
                    match_cfg = mc
                    break

            # Load keyterms from match_data/{id}/keyterms.txt
            keyterms = []
            keyterms_path = os.path.join(self._MATCH_DATA_DIR, match_id, "keyterms.txt")
            if os.path.isfile(keyterms_path):
                try:
                    with open(keyterms_path) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                keyterms.append(line)
                except Exception:
                    pass

            # Current log dir info
            log_dir = getattr(worker, '_log_dir', None)
            log_files = {}
            if log_dir and os.path.isdir(log_dir):
                for fname in os.listdir(log_dir):
                    if fname.endswith(".jsonl"):
                        fpath = os.path.join(log_dir, fname)
                        try:
                            with open(fpath) as f:
                                line_count = sum(1 for _ in f)
                            log_files[fname] = {"lines": line_count}
                        except Exception:
                            log_files[fname] = {"lines": 0}

            # Historical runs from logs/ directory
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logs_base = os.path.join(root, "logs")
            runs = []
            if os.path.isdir(logs_base):
                prefix = f"{match_id}_"
                for d in sorted(os.listdir(logs_base), reverse=True):
                    if d.startswith(prefix) and os.path.isdir(os.path.join(logs_base, d)):
                        runs.append(d)

            s = worker.status
            result = {
                "match_id": match_id,
                "state": s.state,
                "mode": match_cfg.mode if match_cfg else "unknown",
                "stt_utterance_count": s.stt_utterance_count,
                "languages": s.languages,
                "configured_languages": match_cfg.languages if match_cfg else [],
                "error": s.error,
                "started_at": s.started_at,
                "keyterms": keyterms,
                "keyterms_count": len(keyterms),
                "log_dir": log_dir,
                "log_files": log_files,
                "translation_model": self.server_config.translation_model,
                "video_delay": match_cfg.video_delay if match_cfg else None,
                "runs": runs[:20],  # last 20 runs
            }
            self._respond(200, result)
            return

        # Log tailing: /api/matches/{id}/logs/{stt|lang}?tail=N
        m = self._MATCH_LOGS_RE.match(path)
        if m:
            match_id = m.group(1)
            log_key = m.group(2)  # "stt" or language code like "es"
            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            qs = parse_qs(parsed.query)
            tail = int(qs.get("tail", ["100"])[0])
            tail = max(1, min(tail, 500))

            log_dir = getattr(worker, '_log_dir', None)
            if not log_dir:
                self._respond(200, {"match_id": match_id, "log_key": log_key, "rows": []})
                return

            filename = f"{log_key}.jsonl"
            filepath = os.path.join(log_dir, filename)
            if not os.path.isfile(filepath):
                self._respond(200, {"match_id": match_id, "log_key": log_key, "rows": []})
                return

            rows = []
            total_lines = 0
            try:
                with open(filepath, "r") as f:
                    all_lines = f.readlines()
                data_lines = [l for l in all_lines if l.strip()]
                total_lines = len(data_lines)
                for line in reversed(data_lines[-tail:]):
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "header":
                            continue
                        rows.append(obj)
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass

            self._respond(200, {
                "match_id": match_id,
                "log_key": log_key,
                "total_lines": total_lines,
                "rows": rows,
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
    print(f"       GET  /api/matches/{{id}}/logs/{{key}} → log tail (stt or lang)")
    print(f"       POST /api/matches/{{id}}/start       → start a match")
    print(f"       POST /api/matches/{{id}}/stop        → stop a match")
    print(f"       POST /api/token                    → viewer token (single)")
    print(f"       GET  /                             → control.html")
    print(f"       GET  /status.html                  → status page")
    print(f"       GET  /match_detail.html            → match detail page")
    print(f"       GET  /viewer_live.html             → production viewer")
    return server

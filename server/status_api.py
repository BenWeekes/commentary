"""HTTP server for production match control and viewer token generation."""

import json
import os
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

from server.auth import create_session_cookie, verify_session_cookie, parse_cookie
from server.config import get_live_source, get_live_source_channel
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
    _MATCH_REFRESH_RE = re.compile(r'^/api/matches/([^/]+)/refresh-data$')

    def _is_authenticated(self):
        """Check if the request has a valid ops session cookie."""
        cfg = self.server_config
        if not cfg.ops_auth_enabled:
            return True
        cookie_val = parse_cookie(self.headers.get("Cookie", ""), "ops_session")
        if not cookie_val:
            return False
        return verify_session_cookie(cookie_val, cfg.ops_session_secret) is not None

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

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

        # Login page — always accessible
        if path == "/login.html":
            self._serve_file("login.html")
            return

        # Static files — unprotected
        if path == "/viewer_live.html":
            self._serve_file("viewer_live.html")
            return
        if path == "/stt_eval_m05_uni_md33.html":
            self._serve_file("stt_eval_m05_uni_md33.html")
            return
        if path == "/stt_compare_m05_uni_md33_soniox1000.html":
            self._serve_file("stt_compare_m05_uni_md33_soniox1000.html")
            return
        if path == "/stt_compare_m05_uni_md33_soniox1500.html":
            self._serve_file("stt_compare_m05_uni_md33_soniox1500.html")
            return
        if path == "/latency_test.mp4":
            self._serve_file("clips/latency_test/source.mp4", content_type="video/mp4")
            return

        # Root and legacy control.html redirect to status page
        if path in ("/", "/control.html"):
            self._redirect("/status.html")
            return
        # Protected ops pages — redirect to login if auth enabled
        if path == "/status.html":
            if not self._is_authenticated():
                self._redirect("/login.html")
                return
            self._serve_file("status.html")
            return
        if path == "/match_detail.html":
            if not self._is_authenticated():
                self._redirect("/login.html")
                return
            self._serve_file("match_detail.html")
            return

        # Overview: all matches with scheduler state, ordered by config
        if path == "/api/status/overview":
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
            scheduler = self.orchestrator.scheduler
            result = []
            for mc in self.server_config.matches:
                mid = mc.match_id
                ms = scheduler.get_schedule(mid)
                worker = self.orchestrator.get_worker(mid)
                ws = worker.status if worker else None
                live_source = get_live_source(mc)
                source_type = ""
                source_label = ""
                source_detail = ""
                if live_source:
                    source_type = live_source.type
                    if live_source.type == "srt":
                        parsed_source = urlparse(live_source.url)
                        source_label = "SRT pull"
                        source_host = parsed_source.netloc or live_source.url
                        if live_source.ingest_channel:
                            source_detail = f"{source_host} -> {live_source.ingest_channel}"
                        else:
                            source_detail = source_host
                    elif live_source.type in ("srt_direct", "demo_srt_direct"):
                        if live_source.type == "demo_srt_direct":
                            source_label = "Demo SRT direct"
                            source_host = f"127.0.0.1:{live_source.demo_srt_port}"
                        else:
                            parsed_source = urlparse(live_source.url)
                            source_label = "SRT direct"
                            source_host = parsed_source.netloc or live_source.url
                        if live_source.original_channel:
                            source_detail = f"{source_host} -> {live_source.original_channel}"
                        else:
                            source_detail = source_host
                    elif live_source.type == "agora":
                        source_label = "Agora source"
                        source_detail = live_source.channel
                entry = {
                    "match_id": mid,
                    "display_name": mc.display_name or mid,
                    "mode": mc.mode,
                    "enabled": mc.enabled,
                    "auto_manage": mc.auto_manage,
                    "scheduler_state": ms.state if ms else "unknown",
                    "kickoff_utc": ms.kickoff_utc if ms else mc.kickoff_utc,
                    "countdown_seconds": None,
                    "last_refresh_at": ms.last_refresh_at if ms else 0,
                    "last_error": ms.last_error if ms else "",
                    "worker_state": ws.state if ws else "idle",
                    "stt_utterance_count": ws.stt_utterance_count if ws else 0,
                    "configured_languages": mc.languages,
                    "error": ws.error if ws else None,
                    "source_type": source_type,
                    "source_label": source_label,
                    "source_detail": source_detail,
                    "stt_provider": mc.stt_provider,
                    "stt_endpoint_delay_ms": mc.stt_endpoint_delay_ms,
                }
                # Compute countdown
                if ms and ms.kickoff_ts:
                    ttk = ms.kickoff_ts - time.time()
                    entry["countdown_seconds"] = round(ttk)
                result.append(entry)
            self._respond(200, result)
            return

        # All match statuses
        if path == "/api/matches":
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
            statuses = self.orchestrator.get_all_status()
            # Build a config lookup
            cfg_map = {}
            for mc in self.server_config.matches:
                cfg_map[mc.match_id] = mc
            result = {}
            for mid, s in statuses.items():
                mc = cfg_map.get(mid)
                result[mid] = {
                    "match_id": s.match_id,
                    "display_name": mc.display_name if mc else "",
                    "mode": mc.mode if mc else "demo",
                    "enabled": mc.enabled if mc else True,
                    "state": s.state,
                    "stt_utterance_count": s.stt_utterance_count,
                    "languages": s.languages,
                    "configured_languages": mc.languages if mc else [],
                    "stt_provider": mc.stt_provider if mc else "",
                    "stt_endpoint_delay_ms": mc.stt_endpoint_delay_ms if mc else None,
                    "error": s.error,
                    "started_at": s.started_at,
                }
            self._respond(200, result)
            return

        # Single match status (protected)
        m = self._MATCH_STATUS_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
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
                "stt_provider": worker._match.stt_provider,
                "stt_endpoint_delay_ms": worker._match.stt_endpoint_delay_ms,
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

            worker = self.orchestrator.get_worker(match_id)
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
            source_channel = get_live_source_channel(match_cfg)
            if match_cfg.mode == "live" and source_channel:
                # Live: viewer joins the source channel directly
                orig_uid = _next_uid()
                orig_token = generate_viewer_token(
                    cfg.agora_app_id, cfg.agora_app_cert,
                    source_channel, orig_uid)
                channels["original"] = {
                    "channel": source_channel,
                    "token": orig_token,
                    "uid": orig_uid,
                }
            elif worker and "original" in getattr(worker, '_pipelines', {}):
                # Demo: only advertise if the original pipeline is actually running
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

        # Recent English transcript for a match (protected)
        m = self._MATCH_TRANSCRIPT_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
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

        # Match detail: keyterms, log dir, config (protected)
        m = self._MATCH_DETAIL_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
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

            # Read keyterms: prefer runtime (has global fallback), else match_store, else global
            store = self.orchestrator.match_store
            worker_keyterms = getattr(worker, '_keyterms', None)
            if worker_keyterms:
                keyterms = list(worker_keyterms)
                keyterms_source = "runtime"
            else:
                keyterms = store.read_keyterms(match_id)
                if keyterms:
                    keyterms_source = "match_file"
                else:
                    from lib.corrections import TERMS_LIST
                    keyterms = list(TERMS_LIST)
                    keyterms_source = "global_default"

            # Current log dir info
            qs = parse_qs(parsed.query)
            selected_run = qs.get("run", [""])[0]
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

            # Historical runs from match_data/{id}/runs/
            runs = store.list_runs(match_id)

            # Match metadata and roster from match_store
            match_meta = store.read_match_meta(match_id)
            roster_data = store.read_roster(match_id)
            recordings = {}
            recordings_dir = log_dir
            if selected_run:
                recordings_dir = os.path.join(store._match_dir(match_id), "runs", selected_run)
            if recordings_dir:
                recordings_path = os.path.join(recordings_dir, "recordings.json")
                if os.path.isfile(recordings_path):
                    try:
                        with open(recordings_path) as f:
                            recordings = json.load(f)
                    except Exception:
                        recordings = {}

            s = worker.status
            result = {
                "match_id": match_id,
                "display_name": match_cfg.display_name if match_cfg else "",
                "state": s.state,
                "mode": match_cfg.mode if match_cfg else "unknown",
                "enabled": match_cfg.enabled if match_cfg else True,
                "auto_manage": match_cfg.auto_manage if match_cfg else False,
                "kickoff_utc": match_cfg.kickoff_utc if match_cfg else "",
                "stt_utterance_count": s.stt_utterance_count,
                "languages": s.languages,
                "configured_languages": match_cfg.languages if match_cfg else [],
                "error": s.error,
                "started_at": s.started_at,
                "keyterms": keyterms,
                "keyterms_count": len(keyterms),
                "keyterms_source": keyterms_source,
                "log_dir": log_dir,
                "log_files": log_files,
                "selected_run": selected_run,
                "recordings": recordings,
                "translation_model": self.server_config.translation_model,
                "video_delay": match_cfg.video_delay if match_cfg else None,
                "runs": runs[:20],
                "match_meta": match_meta,
                "roster": roster_data,
            }
            self._respond(200, result)
            return

        # Log tailing: /api/matches/{id}/logs/{stt|lang}?tail=N&run=YYYYMMDD_HHMMSS (protected)
        m = self._MATCH_LOGS_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
            match_id = m.group(1)
            log_key = m.group(2)  # "stt" or language code like "es"
            worker = self.orchestrator.get_worker(match_id)
            if not worker:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return

            qs = parse_qs(parsed.query)
            tail = int(qs.get("tail", ["100"])[0])
            tail = max(1, min(tail, 10000))

            # Historical run support: ?run=YYYYMMDD_HHMMSS
            run_name = qs.get("run", [""])[0]
            if run_name:
                # Sanitize — only allow alphanumeric and underscore
                if not re.match(r'^[0-9_]+$', run_name):
                    self._respond(400, {"error": "invalid run name"})
                    return
                store = self.orchestrator.match_store
                log_dir = os.path.join(store._match_dir(match_id), "runs", run_name)
                if not os.path.isdir(log_dir):
                    self._respond(200, {"match_id": match_id, "log_key": log_key, "rows": [], "run": run_name})
                    return
            else:
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

        # Login endpoint — always accessible
        if path == "/api/login":
            cfg = self.server_config
            if not cfg.ops_auth_enabled:
                self._respond(200, {"redirect": "/status.html"})
                return
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid JSON"})
                return
            username = data.get("username", "")
            password = data.get("password", "")
            if username != cfg.ops_username or password != cfg.ops_password:
                self._respond(401, {"error": "invalid credentials"})
                return
            cookie_val = create_session_cookie(username, cfg.ops_session_secret, cfg.ops_session_ttl_hours)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"ops_session={cookie_val}; HttpOnly; SameSite=Lax; Path=/; Max-Age={cfg.ops_session_ttl_hours * 3600}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"redirect": "/status.html"}).encode())
            return

        # Logout endpoint
        if path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "ops_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        # Token endpoint — unprotected (viewer access)
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

        # Start/stop a match (protected)
        m = self._MATCH_ACTION_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
            match_id, action = m.group(1), m.group(2)
            try:
                if action == "start":
                    content_len = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(content_len) if content_len > 0 else b"{}"
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        self._respond(400, {"error": "invalid JSON"})
                        return
                    self.orchestrator.start_match(
                        match_id,
                        stt_provider=data.get("stt_provider"),
                        stt_endpoint_delay_ms=data.get("stt_endpoint_delay_ms"),
                    )
                else:
                    self.orchestrator.stop_match(match_id)
            except KeyError:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return
            except (RuntimeError, ValueError) as e:
                self._respond(400, {"error": str(e)})
                return

            worker = self.orchestrator.get_worker(match_id)
            s = worker.status
            self._respond(200, {
                "match_id": s.match_id,
                "state": s.state,
                "stt_utterance_count": s.stt_utterance_count,
                "languages": s.languages,
                "stt_provider": worker._match.stt_provider,
                "stt_endpoint_delay_ms": worker._match.stt_endpoint_delay_ms,
                "error": s.error,
                "started_at": s.started_at,
            })
            return

        # Refresh SR data for a match (protected)
        m = self._MATCH_REFRESH_RE.match(path)
        if m:
            if not self._is_authenticated():
                self._respond(401, {"error": "authentication required"})
                return
            match_id = m.group(1)
            # Find match config
            match_cfg = None
            for mc in self.server_config.matches:
                if mc.match_id == match_id:
                    match_cfg = mc
                    break
            if not match_cfg:
                self._respond(404, {"error": f"match '{match_id}' not found"})
                return
            if match_cfg.mode == "demo":
                self._respond(400, {"error": "refresh not supported for demo matches"})
                return
            if not match_cfg.sport_event_id:
                self._respond(400, {"error": f"match '{match_id}' has no sport_event_id"})
                return
            # Block refresh while worker is active — data applies on next start
            worker = self.orchestrator.get_worker(match_id)
            if worker and worker.status.state in ("starting", "running"):
                self._respond(409, {"error": "match is running; refresh applies on next start"})
                return

            from server.sr_data import refresh_match_data
            api_key = self.server_config.sportradar_api_key
            if not api_key:
                self._respond(500, {"error": "SPORTRADAR_API_KEY not configured"})
                return

            store = self.orchestrator.match_store
            result = refresh_match_data(match_id, match_cfg, store, api_key)
            code = 200 if result.get("status") == "ok" else 500
            if result.get("status") == "already_refreshing":
                code = 409
            elif result.get("status") in ("no_sport_event_id", "lineups_fetch_failed"):
                code = 502
            self._respond(code, result)
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
    auth_label = "ON" if server_config.ops_auth_enabled else "OFF"
    print(f"[HTTP] Status server on http://localhost:{port} (auth: {auth_label})")
    print(f"       GET  /api/status/overview           → scheduler overview (all matches)")
    print(f"       GET  /api/matches                  → all match statuses")
    print(f"       GET  /api/matches/{{id}}/status      → one match status")
    print(f"       GET  /api/matches/{{id}}/channels    → viewer tokens for all langs")
    print(f"       GET  /api/matches/{{id}}/transcript  → recent English transcript")
    print(f"       GET  /api/matches/{{id}}/logs/{{key}} → log tail (stt or lang, ?run= for history)")
    print(f"       POST /api/matches/{{id}}/start       → start a match")
    print(f"       POST /api/matches/{{id}}/stop        → stop a match")
    print(f"       POST /api/matches/{{id}}/refresh-data → refresh SR data")
    print(f"       POST /api/login                    → authenticate")
    print(f"       POST /api/logout                   → clear session")
    print(f"       POST /api/token                    → viewer token (single)")
    print(f"       GET  /login.html                   → login page")
    print(f"       GET  /status.html                  → status page")
    print(f"       GET  /match_detail.html            → match detail page")
    print(f"       GET  /viewer_live.html             → production viewer")
    return server

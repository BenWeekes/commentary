#!/usr/bin/env python3
"""Tennis-only append-only reviewer feedback service on 127.0.0.1:8092."""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
FEEDBACK = BASE / "feedback"
ROUNDS = FEEDBACK / "rounds.json"
PORT = int(os.environ.get("TENNIS_FEEDBACK_PORT", "8092"))
MAX_BODY = 256 * 1024
MAX_ITEMS = 1000
LOCK = threading.Lock()
ROUTING = {
    "STT": "Deepgram settings, utterance segmentation, and confidence policy",
    "Vision": "detector prompt/schema and literal-observation guards",
    "Tracker": "score transition legality, scoreboard confidence, and corroboration",
    "English": "commentary prompt, grounding, timing, and pacing",
    "French": "French tennis localization prompt",
    "Portuguese": "Brazilian Portuguese tennis localization prompt",
}
# Preserve historical profile scopes for late-ledger retention while accepting
# the current v4 low-latency profiles.
PROFILES = {"10s", "6s", "5s", "2s"}
CLIP_ID = "glinka_mayo_cary_2026_12015_5m"


def safe(value: object, limit: int = 64) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))[:limit]
    return result or "anon"


def clean_text(value: object, limit: int) -> str:
    """Make arbitrary browser text UTF-8 writable, including lone surrogates."""
    if not isinstance(value, str):
        value = str(value or "")
    return value.encode("utf-8", "replace").decode("utf-8")[:limit]


def rounds() -> dict:
    try:
        value = json.loads(ROUNDS.read_text())
    except Exception:
        return {"current": None, "rounds": {}}
    return value if isinstance(value, dict) else {"current": None, "rounds": {}}


def trigger_pin() -> str | None:
    value = os.environ.get("TENNIS_TRIGGER_PIN")
    if value:
        return value.strip()
    # Reuse the review team's existing PIN, not football round state/storage.
    shared = BASE.parent / "ai_commentator" / "feedback" / "pin.txt"
    try:
        return shared.read_text().strip()
    except OSError:
        return None


def digest(version: str) -> dict:
    path = FEEDBACK / version / "comments.jsonl"
    groups: dict[str, dict] = {}
    all_items = []
    total = 0
    if path.exists():
        for submission_index, raw in enumerate(path.read_text(errors="strict").splitlines()):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"malformed feedback ledger at line {submission_index + 1}"
                ) from exc
            if not isinstance(record, dict) or not isinstance(record.get("items"), list):
                raise ValueError(
                    f"malformed feedback record at line {submission_index + 1}"
                )
            for item_index, item in enumerate(record.get("items") or []):
                total += 1
                item_id = f"{version}:{submission_index}:{item_index}"
                column = item.get("column") or "?"
                profile = item.get("profile") or "?"
                key = f"{profile}|{column}"
                group = groups.setdefault(
                    key,
                    {
                        "profile": profile,
                        "column": column,
                        "routes_to": ROUTING.get(column, "operator triage"),
                        "count": 0,
                        "positive": 0,
                        "comments": [],
                    },
                )
                group["count"] += 1
                if "👍 good" in (item.get("tags") or []):
                    group["positive"] += 1
                group["comments"].append(
                    {
                        "feedback_id": item_id,
                        "t": item.get("t"),
                        "by": record.get("reviewer"),
                        "tags": item.get("tags"),
                        "comment": item.get("comment"),
                        "cell_text": item.get("cell_text"),
                    }
                )
                all_items.append(
                    {
                        "feedback_id": item_id,
                        "profile": profile,
                        "column": column,
                        "t": item.get("t"),
                        "by": record.get("reviewer"),
                        "tags": item.get("tags"),
                        "comment": item.get("comment"),
                        "cell_text": item.get("cell_text"),
                    }
                )
    return {
        "total_items": total,
        "all_items": all_items,
        "groups": sorted(groups.values(), key=lambda item: (item["profile"], item["column"])),
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size < 0 or size > MAX_BODY:
            raise ValueError("body too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("body must be an object")
        return value

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/tennis_rounds":
            self.send_json(200, rounds())
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        try:
            self.handle_post()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": f"server error: {type(exc).__name__}"})

    def handle_post(self) -> None:
        data = self.body()
        route = self.path.split("?", 1)[0]
        if route == "/tennis_feedback":
            return self.feedback(data)
        if route == "/tennis_trigger":
            return self.trigger(data)
        self.send_json(404, {"ok": False, "error": "not found"})

    def feedback(self, data: dict) -> None:
        raw_version = data.get("version")
        version = safe(raw_version, 24)
        reviewer = safe(data.get("reviewer"))
        items = data.get("items")
        if raw_version != version or not isinstance(items, list) or not items:
            return self.send_json(400, {"ok": False, "error": "valid version and items[] required"})
        if len(items) > MAX_ITEMS:
            return self.send_json(400, {"ok": False, "error": f"at most {MAX_ITEMS} items allowed"})
        cleaned = []
        for value in items:
            if not isinstance(value, dict):
                continue
            column = clean_text(value.get("column"), 32)
            profile = clean_text(value.get("profile"), 16)
            clip = clean_text(value.get("clip"), 64)
            try:
                timestamp = float(value.get("t"))
            except (TypeError, ValueError):
                return self.send_json(400, {"ok": False, "error": "invalid feedback time"})
            if (
                column not in ROUTING
                or profile not in PROFILES
                or clip != CLIP_ID
                or not math.isfinite(timestamp)
                or not 0 <= timestamp <= 300
            ):
                return self.send_json(400, {"ok": False, "error": "invalid feedback scope"})
            cleaned.append(
                {
                    "t": timestamp,
                    "column": column,
                    "profile": profile,
                    "clip": clip,
                    "cell_text": clean_text(value.get("cell_text"), 400),
                    "tags": [clean_text(tag, 32) for tag in (value.get("tags") or [])[:8]],
                    "comment": clean_text(value.get("comment"), 1000),
                }
            )
        if not cleaned:
            return self.send_json(400, {"ok": False, "error": "no valid items"})
        record = {
            "reviewer": reviewer,
            "version": version,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "items": cleaned,
        }
        with LOCK:
            state = rounds()
            status = (state.get("rounds", {}).get(version) or {}).get("status")
            folder = FEEDBACK / version if status == "open" else FEEDBACK / version / "late"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "comments.jsonl").open(
                "a", encoding="utf-8", errors="replace"
            ) as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if status != "open":
            return self.send_json(
                409,
                {
                    "ok": False,
                    "error": f"round {version} is closed",
                    "current": rounds().get("current"),
                    "stored": "late/comments.jsonl (retained but rejected)",
                },
            )
        self.send_json(200, {"ok": True, "stored": len(cleaned)})

    def trigger(self, data: dict) -> None:
        raw_version = data.get("version")
        version = safe(raw_version, 24)
        reviewer = safe(data.get("triggered_by"))
        supplied = str(data.get("pin") or "").strip()
        expected = trigger_pin()
        if not expected:
            return self.send_json(500, {"ok": False, "error": "trigger PIN is not configured"})
        if not supplied or not hmac.compare_digest(supplied, expected):
            time.sleep(0.5)
            return self.send_json(403, {"ok": False, "error": "wrong PIN"})
        with LOCK:
            state = rounds()
            if raw_version != state.get("current"):
                return self.send_json(409, {"ok": False, "error": "only the current round can close"})
            record = (state.get("rounds") or {}).get(version) or {}
            if record.get("status") != "open":
                return self.send_json(409, {"ok": False, "error": f"round {version} is not open"})
            closed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            reviewed = digest(version)
            record.update(status="closed", closed=closed, triggered_by=reviewer)
            state["rounds"][version] = record
            temporary = ROUNDS.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2))
            os.replace(temporary, ROUNDS)
            work = {
                "version": version,
                "closed": closed,
                "triggered_by": reviewer,
                **reviewed,
                "required_disposition_fields": ["status", "reason", "change", "verification"],
                "work_order": (
                    "triage every item; record accepted/rejected/duplicate with evidence; "
                    "implement accepted changes; run all fixtures and worst-of-three; "
                    "check_feedback.py must pass before publishing the next round"
                ),
            }
            (FEEDBACK / f"trigger_{version}.json").write_text(
                json.dumps(work, indent=2, ensure_ascii=False)
            )
        self.send_json(200, {"ok": True, "closed": version, "items": work["total_items"]})

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    FEEDBACK.mkdir(parents=True, exist_ok=True)
    print(f"tennis feedback server on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

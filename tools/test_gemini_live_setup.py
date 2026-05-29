#!/usr/bin/env python3
"""Minimal Gemini Live setup probe.

Loads `.env`, opens a raw Live API WebSocket, sends setup, and waits for
setupComplete. It does not stream match audio or publish to Agora.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from websockets.sync.client import connect

from lib.v2v.gemini_live import DEFAULT_MODEL, LIVE_ENDPOINT


def _load_dotenv(path: Path):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    _load_dotenv(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set")
        return 2

    setup = {
        "setup": {
            "model": args.model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "systemInstruction": {
                "parts": [{
                    "text": "Translate incoming English football commentary into Spanish. Output only spoken Spanish."
                }]
            },
        }
    }
    url = f"{LIVE_ENDPOINT}?key={quote(api_key)}"
    with connect(url, max_size=16 * 1024 * 1024, ping_interval=None,
                 open_timeout=args.timeout) as ws:
        ws.send(json.dumps(setup))
        raw = ws.recv(timeout=args.timeout)
        msg = json.loads(raw)
        if "setupComplete" in msg or "setup_complete" in msg:
            print(f"Gemini Live setup ok: {args.model}")
            return 0
        print(json.dumps(msg, indent=2)[:4000])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

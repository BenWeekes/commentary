#!/usr/bin/env python3
"""Transcribe the exact clip with Deepgram Nova-3 into review-column JSONL."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import requests

from tennis_common import (
    CLIP,
    CONFIG,
    SHARED_ARTIFACTS as ARTIFACTS,
    assert_football_idle,
    load_env,
    require_env,
)


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["DEEPGRAM_API_KEY"])
    if not CLIP.exists():
        raise SystemExit(f"missing clip: {CLIP}; run prepare_clip.sh first")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tennis_stt_") as folder:
        wav = Path(folder) / "audio.wav"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", str(CLIP),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(wav),
            ],
            check=True,
        )
        assert_football_idle()
        response = requests.post(
            "https://api.deepgram.com/v1/listen",
            params={
                "model": CONFIG["models"]["stt"],
                "language": "en",
                "smart_format": "true",
                "punctuate": "true",
                "utterances": "true",
            },
            headers={
                "Authorization": f"Token {__import__('os').environ['DEEPGRAM_API_KEY']}",
                "Content-Type": "audio/wav",
            },
            data=wav.read_bytes(),
            timeout=360,
        )
        response.raise_for_status()
        payload = response.json()
    raw_path = ARTIFACTS / "stt_raw.json"
    raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    utterances = payload.get("results", {}).get("utterances")
    if not isinstance(utterances, list):
        raise SystemExit("Deepgram response is missing results.utterances[]")
    rows = []
    for item in utterances:
        text = str(item.get("transcript") or "").strip()
        confidence = item.get("confidence")
        if not text or not isinstance(confidence, (int, float)):
            continue
        rows.append(
            {
                "video_time_s": round(float(item["start"]), 3),
                "end_s": round(float(item["end"]), 3),
                "text": text,
                "conf": round(float(confidence), 4),
                "provider": "deepgram",
            }
        )
    out = ARTIFACTS / "stt.jsonl"
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    print(f"wrote {out} ({len(rows)} utterances)")


if __name__ == "__main__":
    main()

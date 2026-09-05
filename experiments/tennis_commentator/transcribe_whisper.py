#!/usr/bin/env python3
"""Independent timestamped Whisper STT comparison for faint court speech."""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

from openai import OpenAI

from tennis_common import (
    CLIP,
    SHARED_ARTIFACTS as ARTIFACTS,
    assert_football_idle,
    load_env,
    require_env,
)


def as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    raise TypeError(f"unexpected transcription response: {type(value).__name__}")


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["OPENAI_API_KEY"])
    if not CLIP.exists():
        raise SystemExit(f"missing clip: {CLIP}")
    cached = ARTIFACTS / "stt_whisper.jsonl"
    cached_raw = ARTIFACTS / "stt_whisper_raw.json"
    if cached.exists() and cached_raw.exists():
        print(f"reusing audited Whisper comparison: {cached}")
        return
    with tempfile.TemporaryDirectory(prefix="tennis_whisper_") as folder:
        audio = Path(folder) / "audio.mp3"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", str(CLIP),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame",
                "-b:a", "64k", "-y", str(audio),
            ],
            check=True,
        )
        with audio.open("rb") as handle:
            assert_football_idle()
            response = OpenAI().audio.transcriptions.create(
                model="whisper-1",
                file=handle,
                language="en",
                prompt=(
                    "ATP tennis in Cary. Players Daniil Glinka and Aidan Mayo. "
                    "Transcribe only audible speech, including umpire score calls."
                ),
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                temperature=0,
                timeout=360,
            )
    payload = as_dict(response)
    (ARTIFACTS / "stt_whisper_raw.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )
    rows = []
    for raw in payload.get("segments") or []:
        segment = as_dict(raw)
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        logprob = segment.get("avg_logprob")
        no_speech = segment.get("no_speech_prob")
        confidence = math.exp(float(logprob)) if isinstance(logprob, (int, float)) else 0.5
        if isinstance(no_speech, (int, float)):
            confidence *= 1.0 - float(no_speech)
        rows.append(
            {
                "video_time_s": round(float(segment.get("start", 0)), 3),
                "end_s": round(float(segment.get("end", 0)), 3),
                "text": text,
                "conf": round(max(0.0, min(1.0, confidence)), 4),
                "provider": "whisper",
            }
        )
    out = ARTIFACTS / "stt_whisper.jsonl"
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    print(f"wrote {out} ({len(rows)} segments)")


if __name__ == "__main__":
    main()

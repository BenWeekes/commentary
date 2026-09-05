#!/usr/bin/env python3
"""Render EN/FR/PT commentary tracks and mux each over the exact source clip."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
import wave
import hashlib
from concurrent.futures import ThreadPoolExecutor
import re

from tennis_common import (
    ARTIFACTS,
    CLIP,
    CONFIG,
    DELAY_S,
    PROFILE,
    assert_football_idle,
    load_env,
    read_jsonl,
    require_env,
)

SR = 16000
VOICES = {
    "en": "kfU9VUUMjY4PWNoUfZ45",
    "fr": "LcKoSBj8CeBInl4bQHtq",
    "pt": "HR2TRGmi4QbMsO5omv7l",
}
WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def tts(text: str, voice: str) -> bytes:
    assert_football_idle()
    body = json.dumps(
        {
            "text": text,
            "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
        }
    ).encode()
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        data=body,
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def write_wave(path, pcm: bytes) -> None:
    temporary = path.with_name(f".{path.stem}.tmp{path.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SR)
            handle.writeframes(pcm)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / (SR * 2)


def tts_duration_limit_s(text: str) -> float:
    """Allow natural delivery while rejecting obvious provider audio glitches."""
    return min(12.0, max(7.5, len(WORD.findall(text)) * 0.75 + 2.0))


def plausible_tts(text: str, pcm: bytes) -> bool:
    duration = pcm_duration_s(pcm)
    return 0.25 <= duration <= tts_duration_limit_s(text)


def placement_start(
    desired: int,
    previous_end: int,
    audio_size: int,
    output_size: int,
) -> int | None:
    """Place ready audio without overlap; spoken duration is not inference time."""
    start = max(desired, previous_end)
    return start if start + audio_size <= output_size else None


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_render_manifest() -> None:
    files = [
        ARTIFACTS / f"{prefix}_{lang}.{suffix}"
        for lang in VOICES
        for prefix, suffix in (("ai", "wav"), ("review", "mp4"))
    ]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise SystemExit("render manifest missing media: " + ", ".join(missing))
    value = {
        "profile": PROFILE,
        "voices": VOICES,
        "files": {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
            for path in files
        },
    }
    out = ARTIFACTS / "render_manifest.json"
    out.write_text(json.dumps(value, indent=2))
    print(f"wrote {out}")


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["ELEVENLABS_API_KEY"])
    source = ARTIFACTS / "commentary_attempt_1.jsonl"
    rows = read_jsonl(source)
    if not rows:
        raise SystemExit(f"missing commentary: {source}")
    buffers = {lang: bytearray(300 * SR * 2) for lang in VOICES}
    ends = {lang: 0 for lang in VOICES}
    delay = DELAY_S
    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in rows:
            if row.get("dropped"):
                continue
            texts = {"en": row.get("text"), "fr": row.get("fr"), "pt": row.get("pt")}
            if not all(isinstance(value, str) and value.strip() for value in texts.values()):
                raise SystemExit(f"missing language at {row.get('video_time_s')}s")
            started = __import__("time").monotonic()
            jobs = {lang: pool.submit(tts, texts[lang], voice) for lang, voice in VOICES.items()}
            pcm = {}
            for lang, job in jobs.items():
                pcm[lang] = job.result()
            retries = {}
            for lang, audio in list(pcm.items()):
                if plausible_tts(texts[lang], audio):
                    retries[lang] = 0
                    continue
                retries[lang] = 1
                pcm[lang] = tts(texts[lang], VOICES[lang])
                if not plausible_tts(texts[lang], pcm[lang]):
                    raise SystemExit(
                        f"implausible {lang} TTS duration at "
                        f"{row.get('video_time_s')}s after retry: "
                        f"{pcm_duration_s(pcm[lang]):.3f}s"
                    )
            tts_latency = __import__("time").monotonic() - started
            prewarmed = bool(CONFIG["timing"].get("prewarm_tts"))
            row["tts_mode"] = (
                "prewarmed_before_match" if prewarmed else "just_in_time"
            )
            row["tts_prewarm_latency_s"] = round(tts_latency, 3) if prewarmed else 0.0
            row["tts_latency_s"] = 0.0 if prewarmed else round(tts_latency, 3)
            row["tts_retries"] = retries
            total = float(row.get("pipeline_latency_s", 0)) + (
                0.0 if prewarmed else tts_latency
            )
            row["end_to_end_latency_s"] = round(total, 3)
            if total > delay:
                row["dropped"] = True
                row["drop_reason"] = "missed_fixed_delay"
                continue
            desired = int((float(row["video_time_s"]) + 0.9) * SR) * 2
            starts = {}
            for lang, audio in pcm.items():
                start = placement_start(
                    desired, ends[lang], len(audio), len(buffers[lang])
                )
                if start is None:
                    row["dropped"] = True
                    row["drop_reason"] = f"{lang}_output_buffer_overrun"
                    break
                starts[lang] = start
            if row.get("dropped"):
                continue
            row["placements"] = {}
            for lang, audio in pcm.items():
                start = starts[lang]
                buffers[lang][start:start + len(audio)] = audio
                ends[lang] = start + len(audio)
                bytes_per_second = SR * 2
                row["placements"][lang] = {
                    "start_s": round(start / bytes_per_second, 3),
                    "end_s": round((start + len(audio)) / bytes_per_second, 3),
                    "duration_s": round(len(audio) / bytes_per_second, 3),
                    "shift_s": round((start - desired) / bytes_per_second, 3),
                }
    source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    for lang, buffer in buffers.items():
        assert_football_idle()
        wav = ARTIFACTS / f"ai_{lang}.wav"
        mp4 = ARTIFACTS / f"review_{lang}.mp4"
        write_wave(wav, bytes(buffer))
        temporary_mp4 = mp4.with_name(f".{mp4.stem}.tmp{mp4.suffix}")
        temporary_mp4.unlink(missing_ok=True)
        filter_graph = (
            "[0:a]highpass=f=250,aformat=channel_layouts=mono,volume=-22dB[crowd];"
            "[1:a]aformat=channel_layouts=mono[comm];"
            "[comm][crowd]amix=inputs=2:duration=first:dropout_transition=0[mix]"
        )
        try:
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                    "-i", str(CLIP), "-i", str(wav),
                    "-filter_complex", filter_graph,
                    "-map", "0:v:0", "-map", "[mix]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k", "-shortest",
                    str(temporary_mp4),
                ],
                check=True,
            )
            os.replace(temporary_mp4, mp4)
        finally:
            temporary_mp4.unlink(missing_ok=True)
        print(f"wrote {mp4}")
    write_render_manifest()


if __name__ == "__main__":
    main()

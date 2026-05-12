#!/usr/bin/env python3
"""Compare Soniox realtime translation with GPT full-turn translation."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.translator import translate_text  # noqa: E402

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def soniox_key() -> str:
    key = os.environ.get("SONIOX_API_KEY", "").strip()
    if key:
        return key
    key_path = Path("/home/ubuntu/soniox")
    if key_path.exists():
        return key_path.read_text().strip()
    raise RuntimeError("SONIOX_API_KEY is not set and /home/ubuntu/soniox is missing")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_keyterms(path: str | None) -> list[str]:
    if not path:
        return []
    terms = []
    seen = set()
    for line in open(path):
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def stream_wav_chunks(path: str, chunk_ms: int = 20,
                      start_s: float = 0.0, duration_s: float | None = None):
    chunk_frames = int(SAMPLE_RATE * chunk_ms / 1000)
    with wave.open(path, "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(
                f"{path} must be 16 kHz mono S16LE WAV; got "
                f"{w.getframerate()}Hz {w.getnchannels()}ch {w.getsampwidth()} bytes/sample"
            )
        if start_s:
            w.setpos(int(start_s * SAMPLE_RATE))
        start = time.time()
        offset = 0.0
        sent_bytes = 0
        max_bytes = int(duration_s * SAMPLE_RATE * BYTES_PER_SAMPLE) if duration_s else None
        while True:
            if max_bytes is not None and sent_bytes >= max_bytes:
                break
            frames = chunk_frames
            if max_bytes is not None:
                remaining_bytes = max_bytes - sent_bytes
                frames = min(frames, max(1, remaining_bytes // BYTES_PER_SAMPLE))
            data = w.readframes(frames)
            if not data:
                break
            if max_bytes is not None and sent_bytes + len(data) > max_bytes:
                data = data[:max_bytes - sent_bytes]
            sent_bytes += len(data)
            yield data, start_s + offset
            offset += len(data) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            sleep_s = start + offset - time.time()
            if sleep_s > 0:
                time.sleep(sleep_s)


def clean_join(tokens: list[dict]) -> str:
    text = "".join(t.get("text", "") for t in tokens if t.get("text") not in ("<end>", "<fin>")).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    return text


def timed_bounds(tokens: list[dict]) -> tuple[float | None, float | None]:
    timed = [t for t in tokens if t.get("start_ms") is not None and t.get("end_ms") is not None]
    if not timed:
        return None, None
    return min(t["start_ms"] for t in timed) / 1000.0, max(t["end_ms"] for t in timed) / 1000.0


def soniox_context(terms: list[str]) -> dict:
    return {
        "general": [
            {"key": "domain", "value": "Bundesliga football commentary"},
            {"key": "task", "value": "Translate live English football commentary"},
        ],
        "terms": terms[:300],
        "text": (
            "Live English football commentary. Prefer exact player, team, venue, "
            "and referee names from the supplied terms when the audio is ambiguous."
        ),
    }


def run_soniox_translation(audio: str, out_dir: Path, lang: str, endpoint_ms: int,
                           keyterms: list[str], duration_s: float | None) -> list[dict]:
    raw_rows: list[dict] = []
    emitted: list[dict] = []
    config = {
        "api_key": soniox_key(),
        "model": "stt-rt-v4",
        "audio_format": "s16le",
        "num_channels": 1,
        "sample_rate": SAMPLE_RATE,
        "language_hints": ["en", lang] if lang != "en" else ["en"],
        "language_hints_strict": True,
        "enable_language_identification": True,
        "enable_speaker_diarization": True,
        "enable_endpoint_detection": True,
        "max_endpoint_delay_ms": endpoint_ms,
        "context": soniox_context(keyterms),
        "translation": {
            "type": "one_way",
            "target_language": lang,
        },
        "client_reference_id": f"translation-eval-{lang}-{endpoint_ms}",
    }

    with connect(SONIOX_WS_URL, max_size=16 * 1024 * 1024) as ws:
        ws.send(json.dumps(config))
        wall_start = time.time()

        def latency_ms(audio_end: float | None) -> int | None:
            if audio_end is None:
                return None
            return round((time.time() - wall_start - audio_end) * 1000)

        def sender():
            try:
                for chunk, _offset in stream_wav_chunks(audio, duration_s=duration_s):
                    ws.send(chunk)
                ws.send("")
            except (ConnectionClosed, OSError):
                return

        send_thread = threading.Thread(target=sender, daemon=True)
        send_thread.start()

        original_tokens: list[dict] = []
        translation_tokens: list[dict] = []
        seen_original = set()
        seen_translation = set()
        last_audio_end: float | None = None

        def emit(reason: str):
            nonlocal original_tokens, translation_tokens, seen_original, seen_translation, last_audio_end
            original = clean_join(original_tokens)
            translated = clean_join(translation_tokens)
            start, end = timed_bounds(original_tokens)
            if end is not None:
                last_audio_end = end
            elif last_audio_end is not None:
                end = last_audio_end
            if original or translated:
                emitted.append({
                    "id": len(emitted) + 1,
                    "source_start": start,
                    "source_end": end,
                    "source": original,
                    "translation": translated,
                    "emit_reason": reason,
                    "soniox_translation_latency_ms": latency_ms(end),
                    "original_token_count": len(original_tokens),
                    "translation_token_count": len(translation_tokens),
                })
            original_tokens = []
            translation_tokens = []
            seen_original.clear()
            seen_translation.clear()

        try:
            while True:
                msg = ws.recv()
                obj = json.loads(msg)
                raw_rows.append(obj)
                if obj.get("error_code"):
                    if not raw_rows[:-1] and not emitted:
                        raise RuntimeError(f"Soniox error {obj.get('error_code')}: {obj.get('error_message')}")
                    print(
                        f"[soniox] warning: ending after error {obj.get('error_code')}: "
                        f"{obj.get('error_message')}",
                        flush=True,
                    )
                    break
                for tok in obj.get("tokens", []):
                    if not tok.get("is_final"):
                        continue
                    status = tok.get("translation_status") or "original"
                    text = tok.get("text")
                    if text in ("<end>", "<fin>"):
                        emit("end_token" if text == "<end>" else "fin_token")
                        continue
                    key = (tok.get("start_ms"), tok.get("end_ms"), text, tok.get("speaker"), status)
                    if status == "translation":
                        if key not in seen_translation:
                            seen_translation.add(key)
                            translation_tokens.append(tok)
                    else:
                        if key not in seen_original:
                            seen_original.add(key)
                            original_tokens.append(tok)
                if obj.get("finished"):
                    break
        except ConnectionClosed:
            pass
        emit("stream_end")
        send_thread.join(timeout=2)

    write_jsonl(out_dir / "soniox_translation_raw.jsonl", raw_rows)
    write_json(out_dir / "soniox_translation_turns.json", emitted)
    return emitted


def run_gpt_translation(turns: list[dict], out_dir: Path, lang: str,
                        model: str, reasoning_effort: str, roster: str | None,
                        max_turns: int | None) -> list[dict]:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    rows = []
    selected = turns[:max_turns] if max_turns else turns
    for i, turn in enumerate(selected, 1):
        source = turn.get("text") or turn.get("source") or ""
        if not source.strip():
            continue
        start = time.time()
        translated = translate_text(
            client,
            source,
            lang,
            model=model,
            reasoning_effort=reasoning_effort,
            roster=roster,
        )
        rows.append({
            "id": i,
            "source_start": turn.get("start") or turn.get("source_start"),
            "source_end": turn.get("end") or turn.get("source_end"),
            "source": source,
            "translation": translated,
            "gpt_translation_latency_ms": round((time.time() - start) * 1000),
        })
        print(f"[gpt] {i}/{len(selected)} {rows[-1]['gpt_translation_latency_ms']}ms {source[:50]!r}", flush=True)
    write_json(out_dir / "gpt_translation_turns.json", rows)
    return rows


def align_by_time(a: list[dict], b: list[dict]) -> list[dict]:
    out = []
    for row in a:
        start = row.get("source_start")
        end = row.get("source_end")
        if start is None or end is None:
            candidates = []
        else:
            candidates = [
                x for x in b
                if x.get("source_end") is not None
                and x.get("source_start") is not None
                and float(x["source_end"]) >= float(start) - 0.5
                and float(x["source_start"]) <= float(end) + 0.5
            ]
        out.append({
            "source_start": start,
            "source_end": end,
            "source": row.get("source"),
            "soniox_translation": row.get("translation", ""),
            "soniox_latency_ms": row.get("soniox_translation_latency_ms"),
            "gpt_translation": " ".join(x.get("translation", "") for x in candidates),
            "gpt_turns": len(candidates),
        })
    return out


def summarize(soniox_rows: list[dict], gpt_rows: list[dict], aligned: list[dict]) -> dict:
    soniox_lat = [x["soniox_translation_latency_ms"] for x in soniox_rows if x.get("soniox_translation_latency_ms") is not None]
    gpt_lat = [x["gpt_translation_latency_ms"] for x in gpt_rows if x.get("gpt_translation_latency_ms") is not None]
    return {
        "soniox_turns": len(soniox_rows),
        "gpt_turns": len(gpt_rows),
        "aligned_rows": len(aligned),
        "soniox_median_latency_ms": statistics.median(soniox_lat) if soniox_lat else None,
        "soniox_p90_latency_ms": statistics.quantiles(soniox_lat, n=10)[8] if len(soniox_lat) >= 10 else None,
        "gpt_median_latency_ms": statistics.median(gpt_lat) if gpt_lat else None,
        "gpt_p90_latency_ms": statistics.quantiles(gpt_lat, n=10)[8] if len(gpt_lat) >= 10 else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav")
    ap.add_argument("--turns", default="match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512/soniox_rt_endpoint1500/turns.json")
    ap.add_argument("--keyterms", default="match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt")
    ap.add_argument("--out", default="match_data/m05_uni_md33/eval/20260510_190915/translation_eval")
    ap.add_argument("--lang", default="es")
    ap.add_argument("--endpoint-ms", type=int, default=1500)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--gpt-max-turns", type=int, default=0)
    ap.add_argument("--gpt-model", default="gpt-5.4")
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--skip-soniox", action="store_true")
    ap.add_argument("--skip-gpt", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    keyterms = load_keyterms(args.keyterms)
    source_turns = json.load(open(args.turns))
    if args.duration:
        source_turns = [t for t in source_turns if float(t.get("start", 0)) <= args.duration]

    soniox_rows = []
    if not args.skip_soniox:
        soniox_rows = run_soniox_translation(args.audio, out_dir, args.lang, args.endpoint_ms, keyterms, args.duration)
    elif (out_dir / "soniox_translation_turns.json").exists():
        soniox_rows = json.load(open(out_dir / "soniox_translation_turns.json"))

    gpt_rows = []
    if not args.skip_gpt:
        gpt_rows = run_gpt_translation(
            source_turns,
            out_dir,
            args.lang,
            args.gpt_model,
            args.reasoning_effort,
            roster="\n".join(keyterms[:300]),
            max_turns=args.gpt_max_turns or None,
        )
    elif (out_dir / "gpt_translation_turns.json").exists():
        gpt_rows = json.load(open(out_dir / "gpt_translation_turns.json"))

    aligned = align_by_time(soniox_rows, gpt_rows)
    summary = summarize(soniox_rows, gpt_rows, aligned)
    summary.update({
        "lang": args.lang,
        "endpoint_ms": args.endpoint_ms,
        "duration": args.duration,
        "gpt_model": args.gpt_model,
        "reasoning_effort": args.reasoning_effort,
    })
    write_json(out_dir / "aligned_translation_compare.json", aligned)
    write_json(out_dir / "summary.json", summary)
    lines = ["# Translation Eval Summary", ""]
    lines.append(f"- Language: `{args.lang}`")
    lines.append(f"- Duration: `{args.duration}` seconds")
    lines.append(f"- Soniox turns: `{summary['soniox_turns']}`")
    lines.append(f"- GPT turns: `{summary['gpt_turns']}`")
    lines.append(f"- Soniox latency median/p90: `{summary['soniox_median_latency_ms']}` / `{summary['soniox_p90_latency_ms']}` ms")
    lines.append(f"- GPT latency median/p90: `{summary['gpt_median_latency_ms']}` / `{summary['gpt_p90_latency_ms']}` ms")
    lines.append("")
    lines.append("| Start | Source | Soniox streaming translation | GPT full-turn translation |")
    lines.append("|---:|---|---|---|")
    for row in aligned[:50]:
        start = row.get("source_start")
        lines.append(
            f"| {start if start is not None else '-'} | "
            f"{str(row.get('source','')).replace('|','/')} | "
            f"{str(row.get('soniox_translation','')).replace('|','/')} | "
            f"{str(row.get('gpt_translation','')).replace('|','/')} |"
        )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print((out_dir / "summary.md").read_text())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run live/realtime STT provider evaluations against a fixed WAV file.

The runner streams the same 16 kHz mono WAV at realtime pace to:

- Deepgram Nova-3 live (listen v1)
- Deepgram Flux live (listen v2)
- Soniox realtime WebSocket

Outputs are written under an eval directory so they can be compared with a
gold reference transcript without changing the production live pipeline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def iso_z(epoch: float) -> str:
    d = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond:06d}" + "Z"


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
        max_bytes = None
        sent_bytes = 0
        if duration_s is not None:
            max_bytes = int(duration_s * SAMPLE_RATE * BYTES_PER_SAMPLE)
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
            target = start + offset
            sleep_s = target - time.time()
            if sleep_s > 0:
                time.sleep(sleep_s)


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def word_tokens(s: str) -> list[str]:
    return norm_text(s).split()


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if x == y else 1),
            ))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def write_wav_segment(src_path: str, start_s: float, duration_s: float | None) -> str:
    """Write a temporary 16 kHz mono WAV segment for the shared live STT core."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()
    with wave.open(src_path, "rb") as src:
        if src.getframerate() != SAMPLE_RATE or src.getnchannels() != 1 or src.getsampwidth() != 2:
            raise ValueError(
                f"{src_path} must be 16 kHz mono S16LE WAV; got "
                f"{src.getframerate()}Hz {src.getnchannels()}ch {src.getsampwidth()} bytes/sample"
            )
        src.setpos(min(src.getnframes(), int(start_s * SAMPLE_RATE)))
        frames = src.getnframes() - src.tell()
        if duration_s is not None:
            frames = min(frames, int(duration_s * SAMPLE_RATE))
        data = src.readframes(frames)
        with wave.open(tmp_path, "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(SAMPLE_RATE)
            dst.writeframes(data)
    return tmp_path


def load_keyterms(path: str | None, limit: int | None = None) -> list[str]:
    if not path:
        return []
    terms = [x.strip() for x in open(path) if x.strip() and not x.startswith("#")]
    seen = set()
    out = []
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if limit and len(out) >= limit:
            break
    return out


def soniox_context(terms: list[str]) -> dict:
    return {
        "general": [
            {"key": "domain", "value": "Bundesliga football commentary"},
            {"key": "match", "value": "Mainz vs Union Berlin"},
        ],
        "terms": terms[:300],
        "text": (
            "Live English football commentary. Prefer exact roster names and "
            "football terminology when audio is ambiguous."
        ),
    }


def regroup_soniox_tokens(tokens: list[dict]) -> list[dict]:
    words: list[dict] = []
    cur: dict | None = None
    for tok in tokens:
        txt = tok.get("text") or ""
        if not txt:
            continue
        if txt == "<end>":
            continue
        start = (tok.get("start_ms") or 0) / 1000.0
        end = (tok.get("end_ms") or 0) / 1000.0
        sp = tok.get("speaker")
        conf = float(tok.get("confidence") or 0.0)
        stripped = txt.strip()
        if re.fullmatch(r"[.,;:?!'’-]+", stripped) and cur is not None:
            cur["word"] += stripped
            cur["end"] = end
            cur["confs"].append(conf)
            continue
        new_word = cur is None or txt.startswith(" ") or cur.get("speaker_raw") != sp
        if new_word:
            if cur and cur["word"].strip():
                cur["confidence"] = statistics.fmean(cur.pop("confs")) if cur["confs"] else 0.0
                words.append(cur)
            cur = {
                "word": stripped,
                "start": start,
                "end": end,
                "speaker_raw": sp,
                "speaker": int(sp) - 1 if str(sp).isdigit() else -1,
                "confs": [conf],
            }
        else:
            cur["word"] += stripped
            cur["end"] = end
            cur["confs"].append(conf)
    if cur and cur["word"].strip():
        cur["confidence"] = statistics.fmean(cur.pop("confs")) if cur["confs"] else 0.0
        words.append(cur)
    return sorted(words, key=lambda w: (w["start"], w["end"]))


def words_to_turns(provider: str, words: list[dict], gap_s: float, source_epoch: float = 0.0) -> list[dict]:
    turns: list[dict] = []
    cur: list[dict] = []
    cur_sp = -1
    last_end: float | None = None

    def emit():
        nonlocal cur
        if not cur:
            return
        text = " ".join(w["word"] for w in cur)
        text = re.sub(r"\s+([,.;:?!])", r"\1", text).strip()
        if not text:
            cur = []
            return
        start = cur[0]["start"]
        end = cur[-1]["end"]
        turns.append({
            "provider": provider,
            "id": len(turns) + 1,
            "speaker": cur_sp,
            "start": start,
            "end": end,
            "source_utc": source_epoch + start if source_epoch else None,
            "source_utc_iso": iso_z(source_epoch + start) if source_epoch else None,
            "text": text,
            "confidence": statistics.fmean([w.get("confidence", 0.0) for w in cur]),
            "words": cur,
        })
        cur = []

    for w in words:
        gap = (w["start"] - last_end) if last_end is not None else 0.0
        if cur and ((w.get("speaker", -1) != cur_sp and w.get("speaker", -1) != -1) or gap >= gap_s):
            emit()
        if not cur:
            cur_sp = w.get("speaker", -1)
        cur.append(w)
        last_end = w["end"]
    emit()
    return turns


def run_deepgram_nova(audio: str, out_dir: Path, keyterms: list[str],
                      endpointing: int, utterance_end_ms: int,
                      max_stt_duration: float, segment_start: float = 0.0,
                      segment_duration: float | None = None) -> list[dict]:
    from lib.stt_pipeline import _run_stt_core

    provider = f"deepgram_nova3_ep{endpointing}_utt{utterance_end_ms}_max{max_stt_duration:g}"
    emitted: list[dict] = []
    stop = threading.Event()
    segment_path = write_wav_segment(audio, segment_start, segment_duration)
    segment_wall_start = time.time()

    def latency_ms(local_end: float) -> int:
        return round((time.time() - segment_wall_start - local_end) * 1000)

    def emit_fn(text, audio_start, audio_end, speaker=None):
        emitted.append({
            "provider": provider,
            "id": len(emitted) + 1,
            "speaker": speaker if speaker is not None else -1,
            "start": segment_start + audio_start,
            "end": segment_start + audio_end,
            "text": text,
            "emit_reason": "shared_core",
            "stt_latency_ms": latency_ms(audio_end),
        })

    try:
        _run_stt_core(
            audio_source=segment_path,
            deepgram_key=os.environ["DEEPGRAM_API_KEY"],
            stop_event=stop,
            emit_fn=emit_fn,
            max_stt_duration=max_stt_duration,
            video_start_fn=None,
            keyterms=keyterms,
            corrections=[],
            log_tag="STT-EVAL NOVA",
            endpointing_ms=endpointing,
            utterance_end_ms=utterance_end_ms,
        )
    finally:
        stop.set()
        try:
            os.unlink(segment_path)
        except OSError:
            pass
    write_json(out_dir / provider / "turns.json", emitted)
    return emitted


def parse_nova_configs(spec: str) -> list[tuple[int, int, float]]:
    configs = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid nova config {item!r}; expected endpoint:utterance_end:max_duration")
        configs.append((int(parts[0]), int(parts[1]), float(parts[2])))
    return configs


def parse_flux_configs(spec: str) -> list[tuple[float, int]]:
    configs = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid flux config {item!r}; expected eot_threshold:eot_timeout_ms")
        configs.append((float(parts[0]), int(parts[1])))
    return configs


def parse_ints(spec: str) -> list[int]:
    return [int(x.strip()) for x in spec.split(",") if x.strip()]


def run_deepgram_flux(audio: str, out_dir: Path, keyterms: list[str],
                      eot_threshold: float, eot_timeout_ms: int,
                      segment_start: float = 0.0,
                      segment_duration: float | None = None) -> list[dict]:
    from deepgram import DeepgramClient
    from deepgram.listen.v2.types import ListenV2TurnInfo, ListenV2FatalError

    provider = f"deepgram_flux_eot{eot_threshold:g}_timeout{eot_timeout_ms}"
    rows: list[dict] = []
    emitted: list[dict] = []
    stop = threading.Event()
    client = DeepgramClient()
    kwargs = {
        "model": "flux-general-en",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "eot_threshold": str(eot_threshold),
        "eot_timeout_ms": str(eot_timeout_ms),
    }
    if keyterms:
        kwargs["keyterm"] = keyterms[:100]
    ctx = client.listen.v2.connect(**kwargs)
    ws = ctx.__enter__()

    def feed():
        for chunk, _offset in stream_wav_chunks(audio, start_s=segment_start, duration_s=segment_duration):
            if stop.is_set():
                break
            ws.send_media(chunk)
        ws.send_close_stream()

    thread = threading.Thread(target=feed, daemon=True)
    thread.start()
    latest: dict[int, dict] = {}
    try:
        for msg in ws:
            row = msg.model_dump() if hasattr(msg, "model_dump") else {}
            if row.get("type") == "Error" or isinstance(msg, ListenV2FatalError):
                rows.append({
                    "type": "fatal_error",
                    "code": row.get("code", getattr(msg, "code", "")),
                    "description": row.get("description", getattr(msg, "description", "")),
                })
                break
            if row.get("type") != "TurnInfo" and not isinstance(msg, ListenV2TurnInfo):
                rows.append({"type": type(msg).__name__, "data": row})
                continue
            rows.append(row)
            idx = int(row.get("turn_index", 0))
            if row.get("transcript"):
                latest[idx] = row
            if row.get("event") == "EndOfTurn":
                src = latest.get(idx, row)
                text = src.get("transcript", "") or row.get("transcript", "")
                if text.strip():
                    emitted.append({
                        "provider": provider,
                        "id": len(emitted) + 1,
                        "speaker": -1,
                        "start": segment_start + float(src.get("audio_window_start") or row.get("audio_window_start") or 0.0),
                        "end": segment_start + float(src.get("audio_window_end") or row.get("audio_window_end") or 0.0),
                        "text": text.strip(),
                        "emit_reason": "EndOfTurn",
                        "end_of_turn_confidence": row.get("end_of_turn_confidence"),
                    })
    finally:
        stop.set()
        ctx.__exit__(None, None, None)
        thread.join(timeout=2)
    write_jsonl(out_dir / provider / "raw.jsonl", rows)
    write_json(out_dir / provider / "turns.json", emitted)
    return emitted


def run_soniox(audio: str, out_dir: Path, soniox_key: str, terms: list[str],
                endpoint_delay_ms: int, segment_start: float = 0.0,
                segment_duration: float | None = None) -> list[dict]:
    provider = f"soniox_rt_endpoint{endpoint_delay_ms}"
    uri = "wss://stt-rt.soniox.com/transcribe-websocket"
    raw_rows: list[dict] = []
    final_tokens: list[dict] = []
    emitted: list[dict] = []
    config = {
        "api_key": soniox_key,
        "model": "stt-rt-v4",
        "audio_format": "pcm_s16le",
        "num_channels": 1,
        "sample_rate": SAMPLE_RATE,
        "language_hints": ["en"],
        "language_hints_strict": True,
        "enable_language_identification": False,
        "enable_speaker_diarization": True,
        "enable_endpoint_detection": True,
        "max_endpoint_delay_ms": endpoint_delay_ms,
        "context": soniox_context(terms),
        "client_reference_id": provider,
    }
    with connect(uri, max_size=16 * 1024 * 1024) as ws:
        ws.send(json.dumps(config))
        segment_wall_start = time.time()

        def latency_ms(local_end: float) -> int:
            return round((time.time() - segment_wall_start - local_end) * 1000)

        def sender():
            try:
                for chunk, _offset in stream_wav_chunks(audio, start_s=segment_start, duration_s=segment_duration):
                    ws.send(chunk)
                ws.send(b"")
            except (ConnectionClosed, OSError):
                return

        send_thread = threading.Thread(target=sender, daemon=True)
        send_thread.start()
        turn_tokens: list[dict] = []
        seen_final = set()

        def emit_turn(tokens: list[dict], reason: str):
            words = regroup_soniox_tokens(tokens)
            if not words:
                return
            text = " ".join(w["word"] for w in words)
            text = re.sub(r"\s+([,.;:?!])", r"\1", text).strip()
            if not text:
                return
            local_start = min(w["start"] for w in words)
            local_end = max(w["end"] for w in words)
            speakers = [w.get("speaker", -1) for w in words if w.get("speaker", -1) != -1]
            speaker = max(set(speakers), key=speakers.count) if speakers else -1
            emitted.append({
                "provider": provider,
                "id": len(emitted) + 1,
                "speaker": speaker,
                "start": segment_start + local_start,
                "end": segment_start + local_end,
                "text": text,
                "emit_reason": reason,
                "stt_latency_ms": latency_ms(local_end),
            })

        try:
            while True:
                msg = ws.recv()
                obj = json.loads(msg)
                raw_rows.append(obj)
                if obj.get("error_code"):
                    break
                for tok in obj.get("tokens", []):
                    if not tok.get("is_final"):
                        continue
                    final_tokens.append(tok)
                    if tok.get("text") in ("<end>", "<fin>"):
                        emit_turn(turn_tokens, "end_token" if tok.get("text") == "<end>" else "fin_token")
                        turn_tokens = []
                        seen_final.clear()
                        continue
                    key = (tok.get("start_ms"), tok.get("end_ms"), tok.get("text"), tok.get("speaker"))
                    if key in seen_final:
                        continue
                    seen_final.add(key)
                    turn_tokens.append(tok)
                if obj.get("finished"):
                    break
        except ConnectionClosed:
            pass
        emit_turn(turn_tokens, "stream_end")
        send_thread.join(timeout=2)
    write_jsonl(out_dir / provider / "raw.jsonl", raw_rows)
    if not emitted:
        words = regroup_soniox_tokens(final_tokens)
        for w in words:
            w["start"] += segment_start
            w["end"] += segment_start
        emitted = words_to_turns(provider, words, gap_s=endpoint_delay_ms / 1000.0)
        for turn in emitted:
            turn["emit_reason"] = "offline_regroup"
            turn["stt_latency_ms"] = None
    write_json(out_dir / provider / "turns.json", emitted)
    return emitted


def score_turns(provider: str, turns: list[dict], gold: list[dict]) -> dict:
    # Score by matching each gold turn to provider text overlapping the same time
    # window with a small tolerance. This captures both text quality and split quality.
    scored = []
    all_ref_words = 0
    all_edits = 0
    max_hyp_end = max((float(t.get("end", 0)) for t in turns), default=0.0)
    gold_scored = [
        g for g in gold
        if float(g.get("start", 0)) <= max_hyp_end + 1.0
    ]
    overlap_counts = []
    exactish_boundary = 0
    late_end_deltas = []
    early_end_deltas = []
    for g in gold_scored:
        a = float(g["start"]) - 0.5
        b = float(g["end"]) + 0.5
        overlapping = [
            t for t in turns
            if float(t.get("end", 0)) >= a and float(t.get("start", 0)) <= b
        ]
        text = " ".join(t["text"] for t in overlapping)
        ref = g["text"]
        ref_words = word_tokens(ref)
        hyp_words = word_tokens(text)
        edits = edit_distance(ref_words, hyp_words)
        all_ref_words += max(1, len(ref_words))
        all_edits += edits
        overlap_counts.append(len(overlapping))
        if overlapping:
            last_end = max(float(t.get("end", 0)) for t in overlapping)
            delta = last_end - float(g["end"])
            if abs(delta) <= 0.75:
                exactish_boundary += 1
            elif delta > 0:
                late_end_deltas.append(delta)
            else:
                early_end_deltas.append(abs(delta))
        scored.append({
            "gold_id": g.get("id"),
            "start": g.get("start"),
            "end": g.get("end"),
            "gold": ref,
            "hyp": text,
            "wer": edits / max(1, len(ref_words)),
            "similarity": similarity(ref, text),
        })
    scored_sorted = sorted(scored, key=lambda x: (x["wer"], -x["similarity"]), reverse=True)
    durations = [float(t.get("end", 0)) - float(t.get("start", 0)) for t in turns]
    texts = [str(t.get("text", "")).strip() for t in turns]
    latencies = [
        float(t["stt_latency_ms"])
        for t in turns
        if t.get("stt_latency_ms") is not None
    ]
    force_splits = [t for t in turns if t.get("emit_reason") == "force_split"]
    short_turns = [d for d in durations if d < 1.0]
    very_short_turns = [d for d in durations if d < 0.6]
    trailing_fragment_re = re.compile(
        r"\b(?:a|an|the|and|or|but|for|to|of|in|on|at|with|from|by|as|that|which|who|when|if|because)$",
        re.IGNORECASE,
    )
    fragment_like = [
        text for text in texts
        if text and (text.endswith(",") or trailing_fragment_re.search(norm_text(text)))
    ]
    split_gold = [n for n in overlap_counts if n > 1]
    return {
        "provider": provider,
        "turn_count": len(turns),
        "gold_turns_scored": len(gold_scored),
        "median_turn_s": statistics.median(durations) if durations else 0,
        "mean_turn_s": statistics.fmean(durations) if durations else 0,
        "short_turn_count": len(short_turns),
        "very_short_turn_count": len(very_short_turns),
        "fragment_like_count": len(fragment_like),
        "split_gold_turn_count": len(split_gold),
        "split_gold_turn_rate": len(split_gold) / max(1, len(gold_scored)),
        "mean_hyp_turns_per_gold": statistics.fmean(overlap_counts) if overlap_counts else 0,
        "boundary_within_750ms_rate": exactish_boundary / max(1, len(gold_scored)),
        "median_late_boundary_s": statistics.median(late_end_deltas) if late_end_deltas else 0,
        "median_early_boundary_s": statistics.median(early_end_deltas) if early_end_deltas else 0,
        "median_stt_latency_ms": statistics.median(latencies) if latencies else None,
        "p90_stt_latency_ms": statistics.quantiles(latencies, n=10)[8] if len(latencies) >= 10 else None,
        "force_split_count": len(force_splits),
        "wer": all_edits / max(1, all_ref_words),
        "mean_similarity": statistics.fmean(x["similarity"] for x in scored) if scored else 0,
        "worst": scored_sorted[:20],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav")
    ap.add_argument("--gold", default="match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json")
    ap.add_argument("--keyterms", default="match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt")
    ap.add_argument("--out", default="match_data/m05_uni_md33/eval/20260510_190915/live_stt")
    ap.add_argument("--providers", default="flux,nova,soniox",
                    help="Comma list: flux,nova,soniox")
    ap.add_argument("--nova-configs", default="500:1500:8",
                    help="Comma list endpoint_ms:utterance_end_ms:max_duration_s")
    ap.add_argument("--flux-configs", default="0.8:2000",
                    help="Comma list eot_threshold:eot_timeout_ms")
    ap.add_argument("--soniox-endpoints", default="1500",
                    help="Comma list of Soniox max_endpoint_delay_ms values")
    ap.add_argument("--segment-seconds", type=float, default=0.0,
                    help="Restart each realtime provider every N seconds and merge timestamps")
    args = ap.parse_args()

    load_dotenv()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    terms = load_keyterms(args.keyterms)
    gold = json.load(open(args.gold))
    selected = {x.strip() for x in args.providers.split(",") if x.strip()}
    results: dict[str, list[dict]] = {}
    threads: list[threading.Thread] = []
    errors: list[dict] = []

    def run_segmented(name: str, provider_label: str, fn):
        duration = wav_duration(args.audio)
        if not args.segment_seconds:
            turns = fn(out_dir, 0.0, None)
            for i, t in enumerate(turns, 1):
                t["id"] = i
            write_json(out_dir / provider_label / "turns.json", turns)
            return turns
        merged: list[dict] = []
        seg = 0
        start_s = 0.0
        while start_s < duration:
            seg_dur = min(args.segment_seconds, duration - start_s)
            seg_out = out_dir / "_segments" / provider_label / f"{seg:03d}_{int(start_s):05d}s"
            print(f"[eval:{name}] segment {seg} {start_s:.1f}-{start_s + seg_dur:.1f}s", flush=True)
            turns = fn(seg_out, start_s, seg_dur)
            merged.extend(turns)
            start_s += seg_dur
            seg += 1
        merged.sort(key=lambda t: (float(t.get("start", 0)), float(t.get("end", 0))))
        for i, t in enumerate(merged, 1):
            t["id"] = i
        write_json(out_dir / provider_label / "turns.json", merged)
        return merged

    def wrap(name: str, provider_label: str, fn):
        def run():
            try:
                results[name] = run_segmented(name, provider_label, fn)
            except Exception as exc:  # keep other providers running
                err = {"provider": name, "error": repr(exc)}
                errors.append(err)
                print(f"[eval:{name}] ERROR {err['error']}", flush=True)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)

    if "nova" in selected:
        for endpointing, utterance_end_ms, max_stt_duration in parse_nova_configs(args.nova_configs):
            label = f"deepgram_nova3_ep{endpointing}_utt{utterance_end_ms}_max{max_stt_duration:g}"
            name = f"nova_{endpointing}_{utterance_end_ms}_{max_stt_duration:g}"
            wrap(
                name,
                label,
                lambda od, ss, sd, ep=endpointing, utt=utterance_end_ms, mx=max_stt_duration:
                    run_deepgram_nova(args.audio, od, terms, ep, utt, mx, ss, sd),
            )
    if "flux" in selected:
        for eot_threshold, eot_timeout_ms in parse_flux_configs(args.flux_configs):
            label = f"deepgram_flux_eot{eot_threshold:g}_timeout{eot_timeout_ms}"
            name = f"flux_{eot_threshold:g}_{eot_timeout_ms}"
            wrap(
                name,
                label,
                lambda od, ss, sd, eot=eot_threshold, tout=eot_timeout_ms:
                    run_deepgram_flux(args.audio, od, terms, eot, tout, ss, sd),
            )
    if "soniox" in selected:
        soniox_key = Path("/home/ubuntu/soniox").read_text().strip()
        for endpoint_delay_ms in parse_ints(args.soniox_endpoints):
            label = f"soniox_rt_endpoint{endpoint_delay_ms}"
            name = f"soniox_{endpoint_delay_ms}"
            wrap(
                name,
                label,
                lambda od, ss, sd, ep=endpoint_delay_ms:
                    run_soniox(args.audio, od, soniox_key, terms, ep, ss, sd),
            )
    if False:
        wrap(
            "nova",
            "deepgram_nova3_ep500_utt1500_max8",
            lambda od, ss, sd: run_deepgram_nova(args.audio, od, terms, 500, 1500, 8.0, ss, sd),
        )
        wrap(
            "flux",
            "deepgram_flux_eot0.8_timeout2000",
            lambda od, ss, sd: run_deepgram_flux(args.audio, od, terms, 0.8, 2000, ss, sd),
        )
        wrap(
            "soniox",
            "soniox_rt_endpoint1500",
            lambda od, ss, sd: run_soniox(args.audio, od, soniox_key, terms, 1500, ss, sd),
        )

    start = time.time()
    while any(t.is_alive() for t in threads):
        elapsed = time.time() - start
        print(f"[eval] running {elapsed:.0f}s / ~1526s; completed={list(results)} errors={len(errors)}", flush=True)
        time.sleep(30)
    for t in threads:
        t.join()

    scores = []
    for name, turns in results.items():
        provider = turns[0]["provider"] if turns else name
        score = score_turns(provider, turns, gold)
        scores.append(score)
        write_json(out_dir / provider / "score.json", score)
    summary = {
        "audio": args.audio,
        "gold": args.gold,
        "keyterms": args.keyterms,
        "started_at": iso_z(start),
        "finished_at": iso_z(time.time()),
        "errors": errors,
        "scores": sorted(scores, key=lambda x: x["wer"]),
    }
    write_json(out_dir / "summary.json", summary)
    md = ["# Live STT Eval Summary", ""]
    md.append("| Provider | Turns | Median Turn | Short | Frag-like | Split Gold | Median Latency | P90 Latency | WER vs Gold | Mean Similarity |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in summary["scores"]:
        md.append(
            f"| {s['provider']} | {s['turn_count']} | {s['median_turn_s']:.2f}s | "
            f"{s['short_turn_count']} | {s['fragment_like_count']} | "
            f"{s['split_gold_turn_rate']:.0%} | "
            f"{s['median_stt_latency_ms'] if s['median_stt_latency_ms'] is not None else '-'}ms | "
            f"{s['p90_stt_latency_ms'] if s['p90_stt_latency_ms'] is not None else '-'}ms | "
            f"{s['wer']:.3f} | {s['mean_similarity']:.3f} |"
        )
    if errors:
        md.append("\n## Errors\n")
        for err in errors:
            md.append(f"- {err['provider']}: `{err['error']}`")
    Path(out_dir / "summary.md").write_text("\n".join(md) + "\n")
    print(Path(out_dir / "summary.md").read_text())


if __name__ == "__main__":
    main()

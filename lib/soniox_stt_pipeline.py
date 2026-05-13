"""Soniox realtime STT pipeline for live PCM commentary."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from lib.audio import pcm_stream_from_pipe
from lib.corrections import GLOBAL_FOOTBALL_CORRECTIONS, apply_corrections
from lib.tts_engine import _ts


SAMPLE_RATE = 16000
SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


def _soniox_key() -> str:
    key = os.environ.get("SONIOX_API_KEY", "").strip()
    if key:
        return key
    key_path = "/home/ubuntu/soniox"
    if os.path.isfile(key_path):
        return open(key_path).read().strip()
    return ""


def _context_for_terms(keyterms: list[str] | None) -> dict:
    terms = []
    seen = set()
    for term in keyterms or []:
        term = term.strip()
        if not term:
            continue
        k = term.lower()
        if k in seen:
            continue
        seen.add(k)
        terms.append(term)
        if len(terms) >= 300:
            break
    return {
        "general": [
            {"key": "domain", "value": "Bundesliga football commentary"},
            {"key": "task", "value": "Transcribe live English football commentary"},
        ],
        "terms": terms,
        "text": (
            "Live football commentary. Prefer exact player, team, venue, and referee "
            "names from the supplied terms when the audio is ambiguous."
        ),
    }


def _clean_join_token_text(tokens: list[dict]) -> str:
    text = "".join(t.get("text", "") for t in tokens).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    return text


def _speaker_for_tokens(tokens: list[dict]):
    labels = [t.get("speaker") for t in tokens if t.get("speaker")]
    if not labels:
        return None
    label, _count = Counter(labels).most_common(1)[0]
    try:
        return int(label) - 1
    except (TypeError, ValueError):
        return label


def _is_terminal_token(tok: dict) -> bool:
    return tok.get("text") in ("<end>", "<fin>")


def _ends_sentence(tok: dict) -> bool:
    return tok.get("text", "").strip().endswith((".", "?", "!"))


def _turn_from_tokens(tokens: list[dict]) -> tuple[str, float, float, object] | None:
    text = _clean_join_token_text(tokens)
    timed = [t for t in tokens if t.get("start_ms") is not None and t.get("end_ms") is not None]
    if not text or not timed:
        return None
    audio_start = min(t["start_ms"] for t in timed) / 1000.0
    audio_end = max(t["end_ms"] for t in timed) / 1000.0
    return text, audio_start, audio_end, _speaker_for_tokens(tokens)


def _word_timings(tokens: list[dict]) -> list[dict]:
    words = []
    current = None
    for tok in tokens:
        raw = tok.get("text", "")
        if not raw or raw.startswith("<"):
            continue
        start_ms = tok.get("start_ms")
        end_ms = tok.get("end_ms")
        if start_ms is None or end_ms is None:
            continue
        clean = raw.strip()
        if not clean:
            continue
        starts_word = raw[:1].isspace() or current is None
        is_punct = re.fullmatch(r"[.,;:?!]+", clean) is not None
        if starts_word and current is not None and not is_punct:
            words.append(current)
            current = None
        if current is None:
            current = {
                "word": clean,
                "start": round(start_ms / 1000.0, 3),
                "end": round(end_ms / 1000.0, 3),
                "speaker": tok.get("speaker"),
                "confidence": tok.get("confidence"),
            }
            continue
        current["word"] += clean
        current["end"] = round(end_ms / 1000.0, 3)
        if tok.get("confidence") is not None:
            if current.get("confidence") is None:
                current["confidence"] = tok.get("confidence")
            else:
                current["confidence"] = min(current["confidence"], tok.get("confidence"))
    if current is not None:
        words.append(current)
    return words


def _utc_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%H:%M:%S", time.gmtime(ts)) + f".{int(ts * 1000) % 1000:03d}Z"


def run_soniox_stt_pipeline_live(audio_pipe, on_utterance, stop_event,
                                 video_start_ref, video_delay=7.0,
                                 keyterms=None, endpoint_delay_ms=1500,
                                 max_stt_duration=6.5,
                                 source_media_start_ref=None,
                                 anchor_ready_event=None,
                                 corrections=None):
    """Run Soniox realtime STT from a live S16LE 16kHz mono PCM pipe.

    Calls on_utterance(text, audio_start, audio_end, play_at, intended_skew_ms=...).
    """
    api_key = _soniox_key()
    if not api_key:
        raise RuntimeError("SONIOX_API_KEY is not set and /home/ubuntu/soniox is missing")

    audio_feed_start_wall = [None]
    audio_feed_start_offset = [None]
    utterance_count = [0]
    corr_list = GLOBAL_FOOTBALL_CORRECTIONS if corrections is None else corrections

    def schedule_anchor():
        if source_media_start_ref and source_media_start_ref[0] is not None:
            return source_media_start_ref[0]
        return audio_feed_start_wall[0]

    def live_play_at(audio_start):
        anchor = schedule_anchor()
        if anchor is not None:
            return anchor + audio_start + video_delay
        vs = video_start_ref[0] if video_start_ref[0] else 0.0
        return vs + audio_start if vs else None

    def intended_skew_ms(audio_start, play_at):
        anchor = schedule_anchor()
        if anchor is None or play_at is None:
            return None
        intended = anchor + audio_start + video_delay
        return round((play_at - intended) * 1000)

    config = {
        "api_key": api_key,
        "model": "stt-rt-v4",
        "audio_format": "pcm_s16le",
        "num_channels": 1,
        "sample_rate": SAMPLE_RATE,
        "language_hints": ["en"],
        "language_hints_strict": True,
        "enable_language_identification": False,
        "enable_speaker_diarization": True,
        "enable_endpoint_detection": True,
        "max_endpoint_delay_ms": int(endpoint_delay_ms),
        "context": _context_for_terms(keyterms),
        "client_reference_id": "commentary-live",
    }

    print("[STT-LIVE] Pipeline: live pipe → Soniox stt-rt-v4 → multi-lang fan-out")
    print(f"[STT-LIVE] Soniox endpoint delay: {endpoint_delay_ms}ms")
    print(f"[STT-LIVE] Soniox max_stt_duration={max_stt_duration}s")
    max_stt_duration_hard = max_stt_duration + 1.0

    with connect(SONIOX_WS_URL, max_size=16 * 1024 * 1024, ping_interval=None) as ws:
        ws.send(json.dumps(config))

        audio_eof_sent = threading.Event()

        def send_audio():
            try:
                for chunk, _offset in pcm_stream_from_pipe(audio_pipe, stop_event):
                    if stop_event.is_set():
                        break
                    ws.send(chunk)
                    if audio_feed_start_wall[0] is None:
                        audio_feed_start_wall[0] = time.time()
                        audio_feed_start_offset[0] = _offset
                        send_anchor = audio_feed_start_wall[0] - _offset
                        if anchor_ready_event is not None:
                            anchor_ready_event.set()
                        vs = video_start_ref[0] if video_start_ref[0] else None
                        if vs:
                            print(f"[STT-LIVE] First PCM sent — {audio_feed_start_wall[0] - vs:.2f}s after video_start")
                        print(f"[STT-LIVE] Soniox send anchor unix={send_anchor:.6f} "
                              f"first_send={audio_feed_start_wall[0]:.6f} "
                              f"pcm_offset={_offset:.3f}s")
                if not stop_event.is_set():
                    ws.send(b"")
                    audio_eof_sent.set()
            except (ConnectionClosed, OSError):
                return

        def emit_turn(tokens: list[dict]):
            turn = _turn_from_tokens(tokens)
            if not turn:
                return
            text, audio_start, audio_end, speaker = turn
            text = apply_corrections(text, corr_list)
            play_at = live_play_at(audio_start)
            anchor = schedule_anchor()
            occurred_at = anchor + audio_start if anchor is not None else None
            occurred_end_at = anchor + audio_end if anchor is not None else None
            print(f"  [{_ts(video_start_ref[0])}] [STT-LIVE SONIOX] "
                  f"audio={audio_start:.1f}-{audio_end:.1f}s speaker={speaker} "
                  f"occurred={_utc_ts(occurred_at)} play={_utc_ts(play_at)} "
                  f"\"{text[:70]}\"")
            on_utterance(
                text, audio_start, audio_end, play_at,
                intended_skew_ms=intended_skew_ms(audio_start, play_at),
                speaker=speaker,
                provider="soniox",
                schedule_anchor_wall=anchor,
                occurred_at=occurred_at,
                occurred_end_at=occurred_end_at,
                word_timings=_word_timings(tokens),
            )
            utterance_count[0] += 1

        def turn_duration(tokens: list[dict]) -> float:
            timed = [t for t in tokens if t.get("start_ms") is not None and t.get("end_ms") is not None]
            if not timed:
                return 0.0
            return (max(t["end_ms"] for t in timed) - min(t["start_ms"] for t in timed)) / 1000.0

        sender = threading.Thread(target=send_audio, daemon=True)
        sender.start()
        turn_tokens: list[dict] = []
        seen_final = set()

        try:
            while not stop_event.is_set():
                msg = ws.recv()
                data = json.loads(msg)
                if data.get("error_code"):
                    raise RuntimeError(f"Soniox error {data.get('error_code')}: {data.get('error_message')}")

                for tok in data.get("tokens", []):
                    if not tok.get("is_final"):
                        continue
                    if _is_terminal_token(tok):
                        emit_turn(turn_tokens)
                        turn_tokens = []
                        seen_final.clear()
                        continue
                    key = (tok.get("start_ms"), tok.get("end_ms"), tok.get("text"), tok.get("speaker"))
                    if key in seen_final:
                        continue
                    seen_final.add(key)
                    turn_tokens.append(tok)
                    duration = turn_duration(turn_tokens)
                    can_soft_split = bool(turn_tokens and _ends_sentence(turn_tokens[-1]))
                    if duration >= max_stt_duration and (can_soft_split or duration >= max_stt_duration_hard):
                        print(f"  [{_ts(video_start_ref[0])}] [STT-LIVE SONIOX SPLIT] "
                              f"force-emitting {duration:.1f}s turn")
                        emit_turn(turn_tokens)
                        turn_tokens = []
                        seen_final.clear()

                if data.get("finished") and audio_eof_sent.is_set():
                    break
        except ConnectionClosed:
            if not audio_eof_sent.is_set() and not stop_event.is_set():
                raise

        emit_turn(turn_tokens)
        stop_event.set()
        sender.join(timeout=2)

    print(f"[STT-LIVE] Soniox pipeline finished — {utterance_count[0]} utterances emitted.")
    return utterance_count[0]

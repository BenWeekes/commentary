#!/usr/bin/env python3
"""Standalone one-language live pipeline smoke test.

This script exercises the same live media path as server live mode:

1. Subscribe to commentary PCM from a source Agora channel/UID
2. Relay delayed video + delayed atmosphere from the source channel
3. Run STT on the commentary PCM
4. Translate into one target language
5. Generate TTS PCM and feed it into the relay publisher stdin
6. Publish the result to a separate output Agora channel

It is intended as a thin harness over the existing subscribe/relay/STT/TTS
components so codec and live-pipeline behavior can be confirmed without
starting the full server.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import openai

from lib.constants import VIDEO_DELAY_S
from lib.corrections import TERMS_LIST
from lib.stt_pipeline import run_stt_pipeline_live
from lib.translator import translate_text, voice_for_lang
from lib.tts_engine import TTSEngine
from server.token_api import generate_viewer_token
from server.match_worker import (
    _kill_publisher,
    _log_pub_stream,
    _load_match_keyterms,
    _wait_for_publisher_signal,
    _wait_for_stderr_signal,
)


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
GO_BASE_DIR = os.path.join(ROOT_DIR, "go-audio-video-publisher")
DEFAULT_SDK_PATH = os.path.join(
    ROOT_DIR, "..", "codex", "server-custom-llm", "go-audio-subscriber",
    "sdk", "agora_sdk_mac",
)


def _load_dotenv(path: str | None = None) -> None:
    if path is None:
        path = os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _read_keyterms(args: argparse.Namespace) -> list[str]:
    if args.keyterms_file:
        terms: list[str] = []
        with open(args.keyterms_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
        return terms or TERMS_LIST
    if args.match_id:
        terms = _load_match_keyterms(args.match_id)
        if terms:
            return terms
    return TERMS_LIST


def _fetch_roster(sport_event_id: str, sportradar_api_key: str) -> str | None:
    if not sport_event_id or not sportradar_api_key:
        return None
    url = (
        "https://api.sportradar.com/soccer-extended/trial/v4/en/"
        f"sport_events/{sport_event_id}/lineups.json"
    )
    req = urllib.request.Request(url, headers={"x-api-key": sportradar_api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    names: list[str] = []
    for team in data.get("sport_event", {}).get("competitors", []):
        for player in team.get("players", []):
            name = player.get("name", "")
            if name:
                names.append(name)
    if not names:
        return None
    return "\n".join(f"- {name}" for name in names)


def _go_program_cmd(binary_name: str, package_path: str) -> list[str]:
    built = os.path.join(GO_BASE_DIR, "bin", binary_name)
    if os.path.isfile(built) and os.access(built, os.X_OK):
        return [built]
    return ["go", "run", package_path]


def _relay_telemetry(lang: str, data: dict) -> None:
    source = data.get("source", "?")
    status = data.get("status", "?")
    text = data.get("text") or ""
    translated = data.get("translated") or ""
    xlat_ms = data.get("translate_time")
    tts_ms = data.get("tts_time")
    xlat_part = f" xlat={round(xlat_ms * 1000)}ms" if xlat_ms else ""
    tts_part = f" tts={round(tts_ms * 1000)}ms" if tts_ms else ""
    preview = translated or text
    if len(preview) > 100:
        preview = preview[:97] + "..."
    print(f"[LIVE-TEST {lang}] {source}/{status}{xlat_part}{tts_part} \"{preview}\"")


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    app_cert = env.get("AGORA_APP_CERT", "")
    if app_cert and "AGORA_APP_CERTIFICATE" not in env:
        env["AGORA_APP_CERTIFICATE"] = app_cert
    if "DYLD_LIBRARY_PATH" not in env:
        env["DYLD_LIBRARY_PATH"] = os.path.abspath(DEFAULT_SDK_PATH)
    return env


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    token = token.strip("-_")
    if not token:
        raise SystemExit("channel token became empty after sanitization")
    return token.lower()


def _write_test_config(
    path: str,
    *,
    match_id: str,
    source_channel: str,
    lang: str,
    sport_event_id: str,
    video_uid: int,
    atmosphere_uid: int,
    commentary_uid: int,
    video_delay: float,
    translation_model: str,
) -> None:
    lines = [
        "# Generated by test_live_pipeline.py",
        f'translation_model: "{translation_model}"',
        "",
        "matches:",
        f"  - match_id: {match_id}",
        "    mode: live",
        f'    sport_event_id: "{sport_event_id}"',
        f"    source_channel: {source_channel}",
        f"    video_uid: {video_uid}",
        f"    atmosphere_uid: {atmosphere_uid}",
        f"    commentary_uid: {commentary_uid}",
        f"    video_delay: {video_delay}",
        f"    languages: [{lang}]",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _build_viewer_test_url(
    *,
    app_id: str,
    app_cert: str,
    channel: str,
    lang: str,
    viewer_uid: int,
    viewer_path: str,
) -> str:
    token = generate_viewer_token(app_id, app_cert, channel, viewer_uid)
    page_uri = Path(viewer_path).resolve().as_uri()
    query = urllib.parse.urlencode({
        "appid": app_id,
        "channel": channel,
        "token": token,
        "uid": viewer_uid,
        "label": f"{channel} ({lang})",
        "autoconnect": "1",
    })
    return f"{page_uri}?{query}"


class _PrefixedPipeReader:
    def __init__(self, pipe, first_chunk: bytes):
        self._pipe = pipe
        self._buf = first_chunk

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data = self._buf
            self._buf = b""
            return data + self._pipe.read()
        if self._buf:
            data = self._buf[:size]
            self._buf = self._buf[size:]
            if len(data) == size:
                return data
            remainder = self._pipe.read(size - len(data))
            return data + (remainder or b"")
        return self._pipe.read(size)


def _wait_for_first_pcm(pipe, proc, stop_event: threading.Event, timeout_s: float = 0.5) -> bytes | None:
    print("[LIVE-TEST] waiting for first commentary PCM from source...")
    last_log = time.time()
    while not stop_event.is_set():
        if proc.poll() is not None:
            raise RuntimeError(f"subscribe_audio exited early with code {proc.returncode}")
        readable, _, _ = select.select([pipe], [], [], timeout_s)
        if readable:
            chunk = pipe.read1(3200)
            if chunk:
                print(f"[LIVE-TEST] first commentary PCM received ({len(chunk)} bytes)")
                return chunk
        now = time.time()
        if now - last_log >= 15:
            print("[LIVE-TEST] still waiting for source audio...")
            last_log = now
    return None


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Standalone one-language live ingest → STT → translate → TTS → relay test"
    )
    parser.add_argument("--source-channel", default="", help="Source Agora channel carrying UID 73/74/75")
    parser.add_argument("--output-channel", default="", help="Output Agora channel to publish the translated test stream to")
    parser.add_argument("--lang", required=True, help="Target language code, for example es/fr/de/pt/tr/en")
    parser.add_argument("--test-id", default="", help="Short id used to derive viewer-compatible channels and match id")
    parser.add_argument("--channel-token", dest="test_id_legacy", default="", help=argparse.SUPPRESS)
    parser.add_argument("--video-uid", type=int, default=73, help="Source video UID")
    parser.add_argument("--atmosphere-uid", type=int, default=74, help="Source atmosphere UID")
    parser.add_argument("--commentary-uid", type=int, default=75, help="Source commentary UID")
    parser.add_argument("--video-delay", type=float, default=VIDEO_DELAY_S, help="Delay applied to video and atmosphere")
    parser.add_argument("--start-margin", type=float, default=5.0, help="Extra seconds before target start to allow relay connection/setup")
    parser.add_argument("--translation-model", default="gpt-5.4-mini", help="OpenAI model for translation")
    parser.add_argument("--max-stt-duration", type=float, default=5.0, help="Deepgram force-split threshold in seconds")
    parser.add_argument("--match-id", default="", help="Optional match id used to load match_data/<match_id>/keyterms.txt")
    parser.add_argument("--sport-event-id", default="", help="Optional Sportradar sport_event_id for roster-aware translation")
    parser.add_argument("--keyterms-file", default="", help="Optional newline-delimited keyterms file; overrides match_id lookup")
    parser.add_argument("--viewer-base-url", default="http://localhost:8080", help="Base URL for printed server-backed viewer_live.html link")
    parser.add_argument("--viewer-test-path", default=os.path.join(ROOT_DIR, "viewer_test.html"), help="Path to standalone viewer HTML for printed watch URL")
    parser.add_argument("--write-test-config", default="", help="Optional path to write a one-match live test config for viewer_live.html")
    parser.add_argument("--prepare-only", action="store_true", help="Write derived config/URLs and exit without starting the pipeline")
    args = parser.parse_args()

    test_id = args.test_id or args.test_id_legacy

    if test_id:
        token = _safe_token(test_id)
        if not args.match_id:
            args.match_id = f"livepipe_{token}"
        if not args.source_channel:
            args.source_channel = f"{args.match_id}_src"
        if not args.output_channel:
            args.output_channel = f"{args.match_id}-{args.lang}"

    if args.match_id and not args.output_channel:
        args.output_channel = f"{args.match_id}-{args.lang}"

    if not args.source_channel:
        raise SystemExit("missing --source-channel (or provide --test-id)")
    if not args.output_channel:
        raise SystemExit("missing --output-channel (or provide --test-id / --match-id)")

    if args.source_channel == args.output_channel:
        raise SystemExit("--output-channel must differ from --source-channel")

    agora_app_id = _required_env("AGORA_APP_ID")
    _required_env("AGORA_APP_CERT")
    deepgram_key = _required_env("DEEPGRAM_API_KEY")
    openai_key = _required_env("OPENAI_API_KEY")
    elevenlabs_key = _required_env("ELEVENLABS_API_KEY")
    app_cert = _required_env("AGORA_APP_CERT")

    stop_event = threading.Event()
    env = _build_env()
    oai_client = openai.OpenAI(api_key=openai_key)
    viewer_uid = 10000 + (int(time.time() * 1000) % 900000)
    standalone_viewer_url = _build_viewer_test_url(
        app_id=agora_app_id,
        app_cert=app_cert,
        channel=args.output_channel,
        lang=args.lang,
        viewer_uid=viewer_uid,
        viewer_path=args.viewer_test_path,
    )

    viewer_url = None
    if args.match_id and args.output_channel == f"{args.match_id}-{args.lang}":
        viewer_url = (
            f"{args.viewer_base_url.rstrip('/')}/viewer_live.html"
            f"?match={args.match_id}&lang={args.lang}"
        )

    if args.write_test_config:
        _write_test_config(
            args.write_test_config,
            match_id=args.match_id or f"livepipe_{_safe_token(args.lang)}",
            source_channel=args.source_channel,
            lang=args.lang,
            sport_event_id=args.sport_event_id,
            video_uid=args.video_uid,
            atmosphere_uid=args.atmosphere_uid,
            commentary_uid=args.commentary_uid,
            video_delay=args.video_delay,
            translation_model=args.translation_model,
        )
        print(f"[LIVE-TEST] wrote config: {args.write_test_config}")
        print(f"[LIVE-TEST] standalone watch url: {standalone_viewer_url}")
        if viewer_url:
            print(f"[LIVE-TEST] server viewer url: {viewer_url}")

    if args.prepare_only:
        print(f"[LIVE-TEST] source channel: {args.source_channel}")
        print(f"[LIVE-TEST] output channel: {args.output_channel}")
        if args.match_id:
            print(f"[LIVE-TEST] match id: {args.match_id}")
        print(f"[LIVE-TEST] standalone watch url: {standalone_viewer_url}")
        return

    try:
        roster = _fetch_roster(args.sport_event_id, os.environ.get("SPORTRADAR_API_KEY", ""))
    except Exception as exc:
        roster = None
        print(f"[LIVE-TEST] roster fetch failed (non-fatal): {exc}")

    keyterms = _read_keyterms(args)

    subscribe_proc = None
    relay_proc = None
    tts = None
    video_start_ref = [None]

    def handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        subscribe_cmd = _go_program_cmd("subscribe_audio", "./cmd/subscribe_audio") + [
            "--app-id", agora_app_id,
            "--channel", args.source_channel,
            "--uid", str(args.commentary_uid),
        ]
        print(
            "[LIVE-TEST] starting subscribe_audio "
            f"channel={args.source_channel} uid={args.commentary_uid}"
        )
        subscribe_proc = subprocess.Popen(
            subscribe_cmd,
            cwd=GO_BASE_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        _wait_for_stderr_signal(
            subscribe_proc,
            "audio subscribing started",
            timeout=15,
            tag="LIVE-TEST SUB",
        )
        threading.Thread(
            target=_log_pub_stream,
            args=(subscribe_proc.stderr, "LIVE-TEST SUB err"),
            daemon=True,
        ).start()

        first_pcm = _wait_for_first_pcm(subscribe_proc.stdout, subscribe_proc, stop_event)
        if first_pcm is None:
            print("[LIVE-TEST] stop requested before any source audio arrived")
            return

        target_start = time.time() + args.start_margin + args.video_delay
        video_start_ref[0] = target_start
        print(
            f"[LIVE-TEST] target_start={target_start:.3f} "
            f"({args.video_delay:.1f}s delay + {args.start_margin:.1f}s margin)"
        )
        print(f"[LIVE-TEST] keyterms={len(keyterms)} lang={args.lang} output={args.output_channel}")
        print(f"[LIVE-TEST] standalone watch url: {standalone_viewer_url}")
        if viewer_url:
            print(f"[LIVE-TEST] server viewer url: {viewer_url}")

        relay_cmd = _go_program_cmd("relay_publish", "./cmd/relay_publish") + [
            "--app-id", agora_app_id,
            "--source-channel", args.source_channel,
            "--output-channel", args.output_channel,
            "--video-uid", str(args.video_uid),
            "--atmos-uid", str(args.atmosphere_uid),
            "--video-delay", str(args.video_delay),
            "--start-at", f"{target_start:.3f}",
        ]
        print(
            "[LIVE-TEST] starting relay_publish "
            f"source={args.source_channel} output={args.output_channel}"
        )
        relay_proc = subprocess.Popen(
            relay_cmd,
            cwd=GO_BASE_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        _wait_for_publisher_signal(
            relay_proc,
            "audio publishing started",
            timeout=15,
            tag="LIVE-TEST RELAY",
        )
        threading.Thread(
            target=_log_pub_stream,
            args=(relay_proc.stderr, "LIVE-TEST RELAY err"),
            daemon=True,
        ).start()

        tts = TTSEngine(
            audio_pipe=relay_proc.stdin,
            voice_id=voice_for_lang(args.lang),
            api_key=elevenlabs_key,
            on_telemetry=lambda data: _relay_telemetry(args.lang, data),
        )
        tts.video_start = target_start
        tts.start()

        def on_utterance(
            text: str,
            audio_start: float,
            audio_end: float,
            play_at: float,
            intended_skew_ms: int | None = None,
        ) -> None:
            def translate_fn(english_text: str):
                voice_id = voice_for_lang(args.lang)
                if args.lang == "en":
                    return (english_text, voice_id)
                translated = translate_text(
                    oai_client,
                    english_text,
                    args.lang,
                    model=args.translation_model,
                    roster=roster,
                )
                return (translated, voice_id)

            play_at_text = f"{play_at:.3f}" if play_at is not None else "-"
            print(
                "[LIVE-TEST STT] "
                f"audio={audio_start:.2f}-{audio_end:.2f}s "
                f"play_at={play_at_text} "
                f"skew={intended_skew_ms}ms \"{text[:100]}\""
            )
            tts.speak(text, play_at=play_at, translate_fn=translate_fn)

        stt_thread = threading.Thread(
            target=run_stt_pipeline_live,
            kwargs=dict(
                audio_pipe=_PrefixedPipeReader(subscribe_proc.stdout, first_pcm),
                on_utterance=on_utterance,
                deepgram_key=deepgram_key,
                stop_event=stop_event,
                video_start_ref=video_start_ref,
                video_delay=args.video_delay,
                max_stt_duration=args.max_stt_duration,
                keyterms=keyterms,
                corrections=[],
            ),
            daemon=True,
        )
        stt_thread.start()

        vs = _wait_for_publisher_signal(
            relay_proc,
            "video delay complete",
            timeout=int(args.start_margin + args.video_delay) + 20,
            tag="LIVE-TEST RELAY",
        )
        # Correct video_start to actual publisher time (mirrors match_worker)
        tts.video_start = vs
        video_start_ref[0] = vs
        print(
            "[LIVE-TEST] relay video live "
            f"(target={target_start:.3f}, actual={vs:.3f}, drift={vs - target_start:+.3f}s)"
        )
        threading.Thread(
            target=_log_pub_stream,
            args=(relay_proc.stdout, "LIVE-TEST RELAY out"),
            daemon=True,
        ).start()

        print(
            "[LIVE-TEST] pipeline running. "
            f"Watch Agora output channel '{args.output_channel}' for language '{args.lang}'."
        )
        while stt_thread.is_alive() and not stop_event.is_set():
            time.sleep(0.5)

        if stop_event.is_set():
            print("[LIVE-TEST] stop requested")
        stt_thread.join(timeout=5)
        print("[LIVE-TEST] STT finished, draining TTS queue...")
        drain_end = time.time() + args.video_delay
        while time.time() < drain_end and not stop_event.is_set():
            time.sleep(0.5)

    finally:
        stop_event.set()
        if tts:
            try:
                tts.stop()
            except Exception:
                pass
        if subscribe_proc:
            _kill_publisher(subscribe_proc, tag="LIVE-TEST SUB")
        if relay_proc:
            _kill_publisher(relay_proc, tag="LIVE-TEST RELAY")


if __name__ == "__main__":
    main()

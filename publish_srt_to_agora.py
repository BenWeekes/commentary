#!/usr/bin/env python3
"""Bridge an SRT input directly into an Agora channel."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.parse

from server.token_api import generate_viewer_token


ROOT_DIR = Path(__file__).resolve().parent
GO_DIR = ROOT_DIR / "go-audio-video-publisher"
DEFAULT_VIEWER_BASE_URL = "https://localhost:8443/commentary/viewer_test.html"

# Preferred per-machine SDK location, populated by go-audio-video-publisher/setup-agora-sdk.sh.
SDK_DIR_LINUX = GO_DIR / "agora-sdk" / "agora_sdk"
SDK_DIR_MAC = GO_DIR / "agora-sdk" / "agora_sdk_mac"

# Legacy locations kept as a fallback so existing dev setups still work.
LEGACY_SDK_PATH_MAC = (
    ROOT_DIR.parent / "codex" / "server-custom-llm" / "go-audio-subscriber" / "sdk" / "agora_sdk_mac"
)


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def build_viewer_url(channel: str, uid: int, viewer_base_url: str) -> str:
    app_id = required_env("AGORA_APP_ID")
    app_cert = required_env("AGORA_APP_CERT")
    token = generate_viewer_token(app_id, app_cert, channel, uid)
    query = urllib.parse.urlencode({
        "appid": app_id,
        "channel": channel,
        "token": token,
        "uid": uid,
        "label": channel,
        "autoconnect": "1",
    })
    sep = "&" if "?" in viewer_base_url else "?"
    return f"{viewer_base_url}{sep}{query}"


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")

    parser = argparse.ArgumentParser(description="Pull SRT and publish into an Agora channel")
    parser.add_argument("--srt-url", required=True, help="SRT input URL to pull from")
    parser.add_argument("--channel", required=True, help="Agora channel to publish into")
    parser.add_argument("--uid", default="73", help="Agora publishing UID")
    parser.add_argument("--video-mode", choices=("yuv", "encoded"), default="yuv", help="Agora video publish mode")
    parser.add_argument("--source-buffer-seconds", type=float, default=0.0,
                        help="Extra wall-clock delay applied before sending source media")
    parser.add_argument("--pcm-listen", default="", help="Local TCP listen address for raw PCM fanout")
    parser.add_argument("--atmos-pcm-listen", default="", help="Local TCP listen address for raw atmosphere PCM fanout")
    parser.add_argument("--video-listen", default="", help="Local TCP listen address for cleaned H264 fanout")
    parser.add_argument("--audio-stream-index", type=int, default=-1,
                        help="Input AV stream index to use for commentary/program audio")
    parser.add_argument("--atmos-audio-stream-index", type=int, default=-1,
                        help="Input AV stream index to use for atmosphere audio")
    parser.add_argument("--viewer-base-url", default=DEFAULT_VIEWER_BASE_URL, help="Base URL for viewer_test.html")
    parser.add_argument("--retry-seconds", type=float, default=5.0, help="Delay before retrying a failed pull")
    parser.add_argument("--max-attempts", type=int, default=0, help="Max attempts before exiting; 0 means retry forever")
    args = parser.parse_args()

    app_id = required_env("AGORA_APP_ID")
    app_cert = required_env("AGORA_APP_CERT")
    env = os.environ.copy()
    env.setdefault("AGORA_APP_CERTIFICATE", app_cert)
    if "DYLD_LIBRARY_PATH" not in env:
        mac_sdk = _first_existing(SDK_DIR_MAC, LEGACY_SDK_PATH_MAC)
        if mac_sdk:
            env["DYLD_LIBRARY_PATH"] = str(mac_sdk.resolve())
    if "LD_LIBRARY_PATH" not in env:
        linux_sdk = _first_existing(SDK_DIR_LINUX)
        if linux_sdk:
            env["LD_LIBRARY_PATH"] = str(linux_sdk.resolve())

    viewer_uid = 560000 + (os.getpid() % 100000)
    viewer_url = build_viewer_url(args.channel, viewer_uid, args.viewer_base_url)
    print(f"[SRT->AGORA] viewer: {viewer_url}")

    cmd = [
        "go", "run", ".",
        "--app-id", app_id,
        "--channel", args.channel,
        "--uid", args.uid,
        "--input", args.srt_url,
        "--video-mode", args.video_mode,
        "--source-buffer-seconds", str(args.source_buffer_seconds),
        "--audio-stream-index", str(args.audio_stream_index),
        "--atmos-audio-stream-index", str(args.atmos_audio_stream_index),
    ]
    if args.pcm_listen:
        cmd.extend(["--pcm-listen", args.pcm_listen])
    if args.atmos_pcm_listen:
        cmd.extend(["--atmos-pcm-listen", args.atmos_pcm_listen])
    if args.video_listen:
        cmd.extend(["--video-listen", args.video_listen])
    print(f"[SRT->AGORA] starting {' '.join(cmd)}")

    attempts = 0
    while True:
        attempts += 1
        print(f"[SRT->AGORA] attempt {attempts}")
        proc = subprocess.Popen(
            cmd,
            cwd=GO_DIR,
            env=env,
            preexec_fn=os.setsid,
        )
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait()
            raise SystemExit(130)

        if returncode == 0:
            raise SystemExit(0)

        if args.max_attempts and attempts >= args.max_attempts:
            raise SystemExit(returncode)

        print(f"[SRT->AGORA] publisher exited with {returncode}; retrying in {args.retry_seconds:.1f}s")
        time.sleep(args.retry_seconds)


if __name__ == "__main__":
    main()

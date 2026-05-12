#!/usr/bin/env python3
"""Submit an audio file to Speechmatics batch API with speaker diarization.

Reads the API key from /home/ubuntu/speechmatics by default.

Usage:
    python3 tools/speechmatics_transcribe.py <audio_file> [options]

Options:
    --output <path>       JSON transcript output path (default: <audio>.json)
    --keyterms <path>     File of names/terms to bias recognition, one per line
    --no-diarize          Disable speaker diarization
    --operating-point std|enhanced   Default enhanced
    --domain <name>       Optional domain hint (e.g. "finance")
    --poll-interval <s>   Seconds between status polls (default 10)
    --key-file <path>     Override key file (default /home/ubuntu/speechmatics)
    --quiet               Suppress progress prints
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


API_BASE = "https://eu1.asr.api.speechmatics.com/v2"
DEFAULT_KEY_FILE = "/home/ubuntu/speechmatics"


def read_key(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def load_keyterms(path: str | None) -> list[dict]:
    if not path:
        return []
    if not os.path.isfile(path):
        sys.stderr.write(f"keyterms file not found: {path}\n")
        return []
    out = []
    for line in open(path):
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        out.append({"content": term})
    return out


def encode_multipart(fields: dict[str, str], file_path: str,
                     file_field: str = "data_file") -> tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body, content_type)."""
    boundary = "----speechmatics-" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(("--" + boundary).encode())
        parts.append(
            (f'Content-Disposition: form-data; name="{name}"').encode())
        parts.append(b"")
        parts.append(value.encode())
    parts.append(("--" + boundary).encode())
    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    mime = mime or "application/octet-stream"
    parts.append(
        (f'Content-Disposition: form-data; name="{file_field}"; '
         f'filename="{filename}"').encode())
    parts.append(f"Content-Type: {mime}".encode())
    parts.append(b"")
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    parts.append(file_bytes)
    parts.append(("--" + boundary + "--").encode())
    parts.append(b"")
    body = crlf.join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def api_request(method: str, path: str, api_key: str, *,
                body: bytes | None = None, content_type: str | None = None,
                query: dict[str, str] | None = None) -> tuple[int, dict | str | bytes]:
    """Make an authenticated API request. Returns (status, parsed_json_or_text)."""
    url = f"{API_BASE}{path}"
    if query:
        from urllib.parse import urlencode
        url += "?" + urlencode(query)
    headers = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(data)
            return resp.status, data
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise SystemExit(
            f"HTTP {e.code} {e.reason} on {method} {url}\n{err_body}") from None


def submit_job(api_key: str, audio_path: str, *,
               diarize: bool, operating_point: str, domain: str | None,
               additional_vocab: list[dict]) -> str:
    config: dict = {
        "type": "transcription",
        "transcription_config": {
            "language": "en",
            "operating_point": operating_point,
            "enable_entities": True,
        },
    }
    if domain:
        config["transcription_config"]["domain"] = domain
    if diarize:
        config["transcription_config"]["diarization"] = "speaker"
        config["transcription_config"]["speaker_diarization_config"] = {
            "prefer_current_speaker": True,
        }
    if additional_vocab:
        # Speechmatics limit: typical caps are roughly 1000 entries; trim defensively.
        config["transcription_config"]["additional_vocab"] = additional_vocab[:1000]

    fields = {"config": json.dumps(config)}
    body, ctype = encode_multipart(fields, audio_path)
    status, resp = api_request(
        "POST", "/jobs", api_key, body=body, content_type=ctype)
    if status != 201 or not isinstance(resp, dict) or "id" not in resp:
        raise SystemExit(f"Submit failed: status={status} resp={resp!r}")
    return resp["id"]


def wait_for_job(api_key: str, job_id: str, *, poll_interval: float,
                 quiet: bool) -> dict:
    """Poll job status until done/rejected/expired; returns final job dict."""
    started = time.time()
    while True:
        status, resp = api_request("GET", f"/jobs/{job_id}", api_key)
        if not isinstance(resp, dict):
            raise SystemExit(f"Unexpected job response: {resp!r}")
        job = resp.get("job") or resp
        state = job.get("status")
        elapsed = time.time() - started
        if not quiet:
            sys.stderr.write(
                f"[{elapsed:6.1f}s] job {job_id} status={state}\n")
            sys.stderr.flush()
        if state in ("done", "rejected", "expired", "deleted"):
            return job
        time.sleep(poll_interval)


def fetch_transcript(api_key: str, job_id: str, fmt: str = "json-v2") -> object:
    status, resp = api_request(
        "GET", f"/jobs/{job_id}/transcript", api_key,
        query={"format": fmt})
    return resp


def render_txt(transcript_json: dict) -> str:
    """Render a speaker-labelled, line-wrapped plain-text view of the json-v2 transcript."""
    lines: list[str] = []
    current_speaker: str | None = None
    line_words: list[str] = []

    def flush():
        if line_words:
            prefix = f"[{current_speaker}] " if current_speaker else ""
            lines.append(prefix + " ".join(line_words))
        line_words.clear()

    for result in transcript_json.get("results", []):
        if result.get("type") not in ("word", "punctuation", "entity"):
            continue
        alt = (result.get("alternatives") or [{}])[0]
        content = alt.get("content", "")
        speaker = alt.get("speaker") or current_speaker
        if speaker != current_speaker:
            flush()
            current_speaker = speaker
        if result.get("type") == "punctuation":
            # Append directly to previous word (no leading space)
            if line_words:
                line_words[-1] = line_words[-1] + content
            else:
                line_words.append(content)
            if result.get("is_eos"):
                flush()
        else:
            line_words.append(content)
    flush()
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="Audio file to transcribe")
    ap.add_argument("--output", default=None,
                    help="Output JSON path (default: <audio>.speechmatics.json)")
    ap.add_argument("--keyterms", default=None,
                    help="Path to one-term-per-line list (becomes additional_vocab)")
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--operating-point", choices=("standard", "enhanced"),
                    default="enhanced")
    ap.add_argument("--domain", default=None, help="e.g. 'finance' (rare for sports)")
    ap.add_argument("--poll-interval", type=float, default=10.0)
    ap.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.audio):
        raise SystemExit(f"audio file not found: {args.audio}")
    api_key = read_key(args.key_file)
    if not api_key:
        raise SystemExit(f"empty key in {args.key_file}")

    additional_vocab = load_keyterms(args.keyterms)
    if not args.quiet:
        sys.stderr.write(
            f"Submitting {args.audio} "
            f"(diarize={not args.no_diarize}, op={args.operating_point}, "
            f"vocab={len(additional_vocab)})\n")

    job_id = submit_job(
        api_key, args.audio,
        diarize=not args.no_diarize,
        operating_point=args.operating_point,
        domain=args.domain,
        additional_vocab=additional_vocab,
    )
    if not args.quiet:
        sys.stderr.write(f"job_id={job_id}\n")

    job = wait_for_job(
        api_key, job_id, poll_interval=args.poll_interval, quiet=args.quiet)
    if job.get("status") != "done":
        raise SystemExit(f"job ended in state {job.get('status')!r}: {job!r}")

    transcript = fetch_transcript(api_key, job_id, fmt="json-v2")

    out_json = args.output or f"{args.audio}.speechmatics.json"
    with open(out_json, "w") as f:
        json.dump(transcript, f, ensure_ascii=False, indent=2)

    out_txt = re.sub(r"\.json$", ".txt", out_json) if out_json.endswith(".json") else out_json + ".txt"
    if isinstance(transcript, dict):
        with open(out_txt, "w") as f:
            f.write(render_txt(transcript))
    if not args.quiet:
        sys.stderr.write(f"wrote {out_json}\nwrote {out_txt}\n")


if __name__ == "__main__":
    main()

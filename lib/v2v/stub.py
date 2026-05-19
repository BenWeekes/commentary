"""Non-provider v2v adapter used to validate orchestration plumbing."""

from __future__ import annotations

import time


def run_v2v_pipeline_live(
    audio_pipe,
    output_audio_writer,
    on_transcript,
    stop_event,
    target_lang,
    video_delay,
    source_media_start_ref,
    voice_id=None,
    provider_options=None,
) -> int:
    provider_options = provider_options or {}
    provider = provider_options.get("provider", "stub")
    if on_transcript:
        on_transcript("system", f"{provider} v2v adapter stub started for {target_lang}", None, None)

    total = 0
    while not stop_event.is_set():
        chunk = audio_pipe.read(320)
        if not chunk:
            break
        total += len(chunk)
        if total == len(chunk) and on_transcript:
            on_transcript("input", "", 0, 10)
        time.sleep(0)

    if on_transcript:
        on_transcript("system", f"{provider} v2v adapter stub stopped for {target_lang}", None, None)
    return total

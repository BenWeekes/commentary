"""Gemini Live voice-to-voice translation adapter.

This uses the raw v1beta WebSocket shape documented for Gemini Live:
setup, realtimeInput.audio with 16 kHz PCM chunks, and serverContent model
turns containing inlineData audio. A prototype-only streaming translation
config can be enabled via provider options, but the default path uses a system
instruction because that is compatible with the public Live API.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from urllib.parse import quote

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from lib.audio import pcm_stream_from_pipe
from lib.v2v.base import language_codes


DEFAULT_MODEL = "models/gemini-3.1-flash-live-preview"
LIVE_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
INPUT_RATE = 16000
OUTPUT_RATE = 16000


def _gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def _gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _parse_rate(mime_type: str | None, default=24000) -> int:
    if not mime_type:
        return default
    marker = "rate="
    if marker not in mime_type:
        return default
    try:
        return int(mime_type.split(marker, 1)[1].split(";", 1)[0])
    except (TypeError, ValueError):
        return default


def _resample_pcm(raw: bytes, source_rate: int, target_rate: int, rate_state):
    if source_rate == target_rate:
        return raw, rate_state
    try:
        import audioop
        converted, rate_state = audioop.ratecv(
            raw,
            2,
            1,
            source_rate,
            target_rate,
            rate_state,
        )
        return converted, rate_state
    except Exception:
        # If resampling is unavailable, emit no malformed audio. The transcript
        # rows and provider error logs still show the session behavior.
        return b"", rate_state


def _decode_output_audio(part: dict, rate_state):
    inline = part.get("inlineData") or part.get("inline_data") or {}
    data = inline.get("data")
    if not data:
        return b"", rate_state
    raw = base64.b64decode(data)
    source_rate = _parse_rate(inline.get("mimeType") or inline.get("mime_type"))
    return _resample_pcm(raw, source_rate, OUTPUT_RATE, rate_state)


def _send_audio_loop(ws, audio_pipe, stop_event, done_event):
    try:
        for chunk, _offset_s in pcm_stream_from_pipe(audio_pipe, stop_event, chunk_ms=10):
            if done_event.is_set() or stop_event.is_set():
                break
            ws.send(json.dumps({
                "realtimeInput": {
                    "audio": {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                },
            }))
        if not stop_event.is_set() and not done_event.is_set():
            ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
    except Exception as e:
        try:
            ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
        except Exception:
            pass
        done_event.set()


def _run_once(
    *,
    audio_pipe,
    output_audio_writer,
    on_transcript,
    stop_event,
    target_lang,
    voice_id,
    provider_options,
):
    api_key = _gemini_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    model = (provider_options or {}).get("model") or _gemini_model()
    target_code = (provider_options or {}).get("target_language_code")
    if not target_code:
        target_code = language_codes(target_lang).get("iso639_1") or target_lang
    echo_target_language = bool((provider_options or {}).get("echo_target_language", True))
    url = f"{LIVE_ENDPOINT}?key={quote(api_key)}"

    generation_config = {
        "responseModalities": ["AUDIO"],
        # The live-translate model used by the prototype accepts this field.
        # For native-audio models that do not, set provider option
        # {"use_streaming_translation_config": false} and provide a system
        # instruction instead.
    }
    use_streaming_translation_config = bool(
        (provider_options or {}).get("use_streaming_translation_config", False)
    )
    if use_streaming_translation_config:
        generation_config["streamingTranslationConfig"] = {
            "targetLanguageCode": target_code,
            "echoTargetLanguage": echo_target_language,
        }
    speech_voice = (provider_options or {}).get("voice_name")
    if speech_voice:
        generation_config["speechConfig"] = {
            "voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": speech_voice},
            },
        }
    setup_body = {
        "model": model,
        "generationConfig": generation_config,
    }
    if not use_streaming_translation_config:
        target_name = language_codes(target_lang).get("name") or target_lang
        setup_body["systemInstruction"] = {
            "parts": [{
                "text": (
                    f"Translate the incoming live English football commentary "
                    f"into {target_name}. Output only natural spoken {target_name}; "
                    "do not answer questions or add commentary."
                )
            }]
        }
        setup_body["inputAudioTranscription"] = {}
        setup_body["outputAudioTranscription"] = {}

    setup = {"setup": setup_body}

    done_event = threading.Event()
    output_rate_state = None
    first_audio_wall = None
    audio_total_ms = 0

    with connect(url, max_size=16 * 1024 * 1024, ping_interval=None) as ws:
        ws.send(json.dumps(setup))
        sender = threading.Thread(
            target=_send_audio_loop,
            args=(ws, audio_pipe, stop_event, done_event),
            daemon=True,
        )

        while not stop_event.is_set():
            raw = ws.recv()
            message = json.loads(raw)

            if "error" in message:
                raise RuntimeError(f"Gemini error: {message['error']}")

            if "setupComplete" in message or "setup_complete" in message:
                if on_transcript:
                    on_transcript("system", f"gemini setup complete: {target_code}", None, None)
                sender.start()
                continue

            content = message.get("serverContent") or {}
            input_text = (content.get("inputTranscription") or {}).get("text")
            if input_text and on_transcript:
                on_transcript("input", input_text, None, None)

            output_text = (content.get("outputTranscription") or {}).get("text")
            if output_text and on_transcript:
                on_transcript("output", output_text, None, None)

            if content.get("interrupted") and output_audio_writer:
                output_audio_writer.write(b"", metadata={"interrupted": True})

            parts = ((content.get("modelTurn") or {}).get("parts") or [])
            for part in parts:
                pcm, output_rate_state = _decode_output_audio(part, output_rate_state)
                if not pcm:
                    continue
                if first_audio_wall is None:
                    first_audio_wall = time.time()
                audio_total_ms += round(len(pcm) / (OUTPUT_RATE * 2) * 1000)
                if output_audio_writer:
                    output_audio_writer.write(pcm, metadata={
                        "source": "v2v_gemini",
                        "voice_id": voice_id,
                        "translated": output_text or "",
                        "v2v_first_audio_wall": first_audio_wall,
                        "v2v_total_audio_ms": audio_total_ms,
                        "provider_session_id": "",
                    })

            if content.get("turnComplete") and on_transcript:
                on_transcript("turn_complete", "", None, None)

            if done_event.is_set():
                break

        done_event.set()
        try:
            sender.join(timeout=1.0)
        except RuntimeError:
            pass

    return audio_total_ms


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
    """Run Gemini Live translation for one target language.

    Reconnect is best-effort for transient WebSocket closures. The current
    source PCM pipe is live-only, so reconnect resumes from the current audio
    position rather than replaying audio already consumed.
    """
    provider_options = provider_options or {}
    reconnect_delay_s = float(provider_options.get("reconnect_delay_s", 1.0))
    max_reconnects = int(provider_options.get("max_reconnects", 3))
    reconnects = 0
    total_audio_ms = 0

    while not stop_event.is_set():
        try:
            total_audio_ms += _run_once(
                audio_pipe=audio_pipe,
                output_audio_writer=output_audio_writer,
                on_transcript=on_transcript,
                stop_event=stop_event,
                target_lang=target_lang,
                voice_id=voice_id,
                provider_options=provider_options,
            )
            break
        except (ConnectionClosed, OSError, TimeoutError) as e:
            reconnects += 1
            if on_transcript:
                on_transcript("system", f"gemini reconnect {reconnects}: {e}", None, None)
            if reconnects > max_reconnects or stop_event.is_set():
                raise
            stop_event.wait(reconnect_delay_s)

    return total_audio_ms

"""Shared contracts for live speech translation provider experiments.

This module intentionally contains no vendor SDK calls. Provider-specific
implementations should plug into these contracts so live-demo runs can compare
quality and latency without changing the match orchestration shape.
"""

from dataclasses import dataclass, field


PIPELINE_STT_TRANSLATE_TTS = "stt_translate_tts"
PIPELINE_VOICE_TO_VOICE = "voice_to_voice"

SUPPORTED_PIPELINE_MODES = {
    PIPELINE_STT_TRANSLATE_TTS,
    PIPELINE_VOICE_TO_VOICE,
}

VOICE_TO_VOICE_PROVIDERS = {
    "openai",
    "gemini",
    "xai",
}


def normalize_pipeline_mode(mode: str | None) -> str:
    value = (mode or PIPELINE_STT_TRANSLATE_TTS).strip().lower().replace("-", "_")
    aliases = {
        "current": PIPELINE_STT_TRANSLATE_TTS,
        "baseline": PIPELINE_STT_TRANSLATE_TTS,
        "stt": PIPELINE_STT_TRANSLATE_TTS,
        "stt_translate_tts": PIPELINE_STT_TRANSLATE_TTS,
        "speech_to_speech": PIPELINE_VOICE_TO_VOICE,
        "voice_to_voice": PIPELINE_VOICE_TO_VOICE,
        "v2v": PIPELINE_VOICE_TO_VOICE,
    }
    return aliases.get(value, value)


def normalize_speech_translation_provider(provider: str | None) -> str:
    value = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "google": "gemini",
        "x_ai": "xai",
        "grok": "xai",
    }
    return aliases.get(value, value)


def validate_pipeline_config(mode: str | None, provider: str | None = "") -> tuple[str, str]:
    """Return normalized (mode, provider), or raise ValueError."""
    normalized_mode = normalize_pipeline_mode(mode)
    normalized_provider = normalize_speech_translation_provider(provider)
    if normalized_mode not in SUPPORTED_PIPELINE_MODES:
        raise ValueError(f"unknown pipeline_mode '{mode}'")
    if normalized_mode == PIPELINE_VOICE_TO_VOICE:
        if not normalized_provider:
            raise ValueError("speech_translation_provider is required for voice_to_voice mode")
        if normalized_provider not in VOICE_TO_VOICE_PROVIDERS:
            raise ValueError(
                f"unknown speech_translation_provider '{provider}' "
                f"(expected one of: {', '.join(sorted(VOICE_TO_VOICE_PROVIDERS))})"
            )
    return normalized_mode, normalized_provider


def provider_display_name(mode: str, provider: str = "") -> str:
    if mode == PIPELINE_STT_TRANSLATE_TTS:
        return "STT + translation + TTS"
    labels = {
        "openai": "OpenAI voice-to-voice",
        "gemini": "Gemini voice-to-voice",
        "xai": "x.ai voice-to-voice",
    }
    return labels.get(provider, provider or mode)


@dataclass
class LiveTranslationEvent:
    """Provider event shape for future voice-to-voice adapters."""

    language: str
    event_type: str
    audio: bytes = b""
    source_text: str = ""
    translated_text: str = ""
    audio_start: float | None = None
    audio_end: float | None = None
    provider_latency_ms: int | None = None
    metadata: dict = field(default_factory=dict)


class LiveSpeechTranslator:
    """Interface implemented by future vendor-specific live providers."""

    provider_name = "base"

    def start(self, audio_source, *, language: str, callbacks, stop_event):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

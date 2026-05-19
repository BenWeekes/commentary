"""Shared contracts and helpers for voice-to-voice adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


V2V_MODE_PREFIX = "v2v_"
CLASSIC_MODE = "classic"
SUPPORTED_V2V_PROVIDERS = {"openai", "gemini", "xai", "stub"}


LANGUAGE_MAP = {
    "en": {"iso639_1": "en", "bcp47": "en-US", "name": "English"},
    "es": {"iso639_1": "es", "bcp47": "es-ES", "name": "Spanish"},
    "pt": {"iso639_1": "pt", "bcp47": "pt-PT", "name": "Portuguese"},
    "fr": {"iso639_1": "fr", "bcp47": "fr-FR", "name": "French"},
    "tr": {"iso639_1": "tr", "bcp47": "tr-TR", "name": "Turkish"},
    "de": {"iso639_1": "de", "bcp47": "de-DE", "name": "German"},
    "it": {"iso639_1": "it", "bcp47": "it-IT", "name": "Italian"},
    "nl": {"iso639_1": "nl", "bcp47": "nl-NL", "name": "Dutch"},
    "ar": {"iso639_1": "ar", "bcp47": "ar-SA", "name": "Arabic"},
}


def normalize_language_mode(mode: str | None, provider: str | None = None) -> tuple[str, str]:
    value = (mode or CLASSIC_MODE).strip().lower().replace("-", "_")
    provider_value = (provider or "").strip().lower().replace("-", "_")
    provider_aliases = {
        "google": "gemini",
        "x_ai": "xai",
        "grok": "xai",
    }
    provider_value = provider_aliases.get(provider_value, provider_value)
    aliases = {
        "stt": CLASSIC_MODE,
        "stt_translate_tts": CLASSIC_MODE,
        "baseline": CLASSIC_MODE,
        "current": CLASSIC_MODE,
    }
    value = aliases.get(value, value)
    if value == CLASSIC_MODE:
        return CLASSIC_MODE, ""
    if value in ("voice_to_voice", "v2v"):
        if not provider_value:
            raise ValueError("v2v language mode requires provider")
        value = f"{V2V_MODE_PREFIX}{provider_value}"
    if value.startswith(V2V_MODE_PREFIX):
        provider_name = provider_value or value[len(V2V_MODE_PREFIX):]
        provider_name = provider_aliases.get(provider_name, provider_name)
        if provider_name not in SUPPORTED_V2V_PROVIDERS:
            raise ValueError(f"unknown v2v provider '{provider_name}'")
        return f"{V2V_MODE_PREFIX}{provider_name}", provider_name
    raise ValueError(f"unknown language mode '{mode}'")


def language_codes(lang: str) -> dict:
    key = (lang or "").strip()
    normalized = key.lower()
    base = normalized.split("-")[0]
    mapped = LANGUAGE_MAP.get(normalized) or LANGUAGE_MAP.get(base)
    if mapped:
        result = dict(mapped)
    else:
        result = {
            "iso639_1": base or normalized,
            "bcp47": key,
            "name": key,
        }
    result["app"] = key
    return result


@dataclass
class V2VHealth:
    provider: str
    state: str = "idle"
    reconnect_count: int = 0
    buffered_audio_ms: int = 0
    first_audio_latency_ms: int | None = None
    last_transcript_at: float | None = None
    last_error: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "state": self.state,
            "reconnect_count": self.reconnect_count,
            "buffered_audio_ms": self.buffered_audio_ms,
            "first_audio_latency_ms": self.first_audio_latency_ms,
            "last_transcript_at": self.last_transcript_at,
            "last_transcript_age": (
                round(time.time() - self.last_transcript_at, 1)
                if self.last_transcript_at else None
            ),
            "last_error": self.last_error,
            **self.metadata,
        }


@dataclass
class V2VTranscript:
    role: str
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    translated_text: str = ""
    metadata: dict = field(default_factory=dict)

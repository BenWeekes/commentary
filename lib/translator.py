# ─── Translation ─────────────────────────────────────────────────────────

LANG_NAMES = {
    "es": "Spanish (Latin American)", "fr": "French", "de": "German",
    "pt": "Portuguese (Brazilian)", "it": "Italian", "ar": "Arabic",
    "ja": "Japanese", "ko": "Korean", "zh": "Mandarin Chinese", "hi": "Hindi",
    "tr": "Turkish", "en": "English",
}

# ElevenLabs voice IDs per language
LANG_VOICES = {
    "es": "jdSy6qWNc1T4C8czPgat",
    "fr": "LcKoSBj8CeBInl4bQHtq",
    "de": "g8JjujAzgjLre020BW2u",
    "pt": "HR2TRGmi4QbMsO5omv7l",
    "zh": "ImsA1Fn5TNc843fFdz99",
    "en": "gU0LNdkMOQCOrPrwtbee",
    "hi": "LcKoSBj8CeBInl4bQHtq",
    "tr": "ImsA1Fn5TNc843fFdz99",
}
DEFAULT_VOICE_ID = "ImsA1Fn5TNc843fFdz99"


def voice_for_lang(lang):
    return LANG_VOICES.get(lang, DEFAULT_VOICE_ID)


TRANSLATE_SYSTEM_WITH_ROSTER = """Translate the English football commentary to {lang_name}.
Fix any misspelled player/team/venue names using the roster below, then translate.

PLAYER ROSTER:
{roster}

Rules:
1. Translate EXACTLY what is said — do not add, remove, or rewrite words
2. Fix misspelled names from the roster, keep all other names unchanged
3. Use natural football terminology for the target language
4. Return ONLY the translation, nothing else
5. Match the length and structure of the original
6. Use correct grammar — never invent word forms"""

TRANSLATE_SYSTEM = """Translate the English football commentary to {lang_name}.
Rules:
1. Translate EXACTLY what is said — do not add, remove, or rewrite words
2. Keep player names, team names, and proper nouns unchanged
3. Use natural football terminology for the target language
4. Return ONLY the translation, nothing else
5. Match the length and structure of the original
6. Use correct grammar — never invent word forms"""


def translate_text(oai_client, text, lang, model="gpt-5.4-mini",
                    reasoning_effort="medium", roster=None):
    lang_name = LANG_NAMES.get(lang, lang)
    if roster:
        system = TRANSLATE_SYSTEM_WITH_ROSTER.format(
            lang_name=lang_name, roster=roster)
    else:
        system = TRANSLATE_SYSTEM.format(lang_name=lang_name)
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    )
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["max_completion_tokens"] = 512
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 512
    resp = oai_client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()

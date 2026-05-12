import json
import re
from difflib import SequenceMatcher

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


def _is_reasoning_model(model):
    """Check if a model supports reasoning_effort (o-series and gpt-5.4+)."""
    if not model:
        return False
    m = model.lower()
    # o1, o3, o4-mini, etc.
    if m.startswith("o") and len(m) > 1 and m[1].isdigit():
        return True
    # gpt-5.4-mini, gpt-5.4, etc.
    if "5.4" in m or "5.5" in m:
        return True
    return False


def translate_text(oai_client, text, lang, model="gpt-5.4",
                    reasoning_effort="low", roster=None):
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
    if reasoning_effort and _is_reasoning_model(model):
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["max_completion_tokens"] = 512
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 512
    resp = oai_client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()


NAME_CORRECTION_SYSTEM = """You correct proper names in English football commentary.

Use ONLY the supplied roster/keyterms. Your job is narrow:
1. Fix misspelled player, team, venue, manager, or referee names.
2. Preserve every non-name word exactly.
3. Do not improve grammar.
4. Do not translate.
5. Do not fix football actions, tactics, score state, or general STT mishears.
6. If uncertain, leave the text unchanged.

Return compact JSON only:
{"text":"corrected English text","corrections":[{"from":"heard","to":"name"}]}"""


def _extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def correct_names_text(oai_client, text, roster=None, keyterms=None,
                       model="gpt-5.4", reasoning_effort="low"):
    """Correct only roster/keyterm proper names in English STT text."""
    terms = []
    seen = set()
    if roster:
        for line in roster.splitlines():
            term = line.lstrip("- ").strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
    for term in keyterms or []:
        term = str(term).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    if not terms:
        return text, []

    names = "\n".join(f"- {term}" for term in terms[:400])
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": NAME_CORRECTION_SYSTEM},
            {"role": "user", "content": f"Roster/keyterms:\n{names}\n\nSTT text:\n{text}"},
        ],
    )
    if reasoning_effort and _is_reasoning_model(model):
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["max_completion_tokens"] = 512
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 512
    resp = oai_client.chat.completions.create(**kwargs)
    data = _extract_json_object(resp.choices[0].message.content)
    corrected = str(data.get("text", text)).strip() or text
    corrections = data.get("corrections") or []
    if not isinstance(corrections, list):
        corrections = []
    return corrected, corrections


def _name_terms(roster=None, keyterms=None):
    terms = []
    seen = set()
    if roster:
        for line in roster.splitlines():
            term = line.lstrip("- ").strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
    for term in keyterms or []:
        term = str(term).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def _norm_name(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _similar(a, b):
    return SequenceMatcher(None, _norm_name(a), _norm_name(b)).ratio()


def _split_token(token):
    m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z-]*)([^A-Za-z]*)$", token)
    if not m:
        return "", token, ""
    return m.group(1), m.group(2), m.group(3)


def correct_names_text_code(text, roster=None, keyterms=None):
    """Conservatively fix obvious proper-name STT errors from roster/keyterms.

    This intentionally only touches capitalized name-like tokens and known
    full-name patterns. It does not try to fix general STT mishearings.
    """
    terms = _name_terms(roster=roster, keyterms=keyterms)
    if not terms or not text:
        return text, []

    full_names = []
    single_names = []
    surname_to_full = {}
    for term in terms:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", term)
        if not words:
            continue
        if len(words) >= 2 and "," not in term:
            full = " ".join(words)
            full_names.append(full)
            surname_to_full.setdefault(_norm_name(words[-1]), full)
        elif len(words[0]) >= 4:
            single_names.append(words[0])
    known_single_norms = {_norm_name(n) for n in single_names}
    stop_prefixes = {
        "and", "as", "at", "for", "from", "heres", "here", "its", "now",
        "so", "theres", "there", "well", "yeah", "yes",
    }

    corrected = text
    corrections = []

    def record(before, after):
        if before != after:
            corrections.append({"from": before, "to": after})

    # Soniox sometimes emits "Sota, Kawasaki," for "Sota Kawasaki".
    for full in full_names:
        words = full.split()
        if len(words) != 2:
            continue
        first, last = map(re.escape, words)
        pattern = re.compile(rf"\b({first})\s*,\s*({last})\s*,?\b", re.IGNORECASE)
        def repl(m, full=full):
            record(m.group(0), full)
            return full
        corrected = pattern.sub(repl, corrected)

    raw_tokens = corrected.split()
    out = []
    i = 0
    while i < len(raw_tokens):
        if i + 1 < len(raw_tokens):
            p1, w1, s1 = _split_token(raw_tokens[i])
            p2, w2, s2 = _split_token(raw_tokens[i + 1])
            if w1[:1].isupper() and w2[:1].isupper():
                surname_full = surname_to_full.get(_norm_name(w2))
                w1_norm = _norm_name(w1)
                if (surname_full and w1_norm not in known_single_norms
                        and w1_norm not in stop_prefixes
                        and _norm_name(w1 + w2) != _norm_name(surname_full)):
                    before = f"{w1} {w2}"
                    record(before, surname_full)
                    out.append(f"{p1}{surname_full}{s2}")
                    i += 2
                    continue

        prefix, word, suffix = _split_token(raw_tokens[i])
        replacement = None
        if word[:1].isupper() and len(word) >= 4 and "'" not in raw_tokens[i]:
            best = None
            best_score = 0.0
            for term in single_names:
                score = _similar(word, term)
                if score > best_score:
                    best = term
                    best_score = score
            if best and best != word and best_score >= 0.86:
                replacement = best

        if replacement:
            record(word, replacement)
            out.append(f"{prefix}{replacement}{suffix}")
        else:
            out.append(raw_tokens[i])
        i += 1

    return " ".join(out), corrections

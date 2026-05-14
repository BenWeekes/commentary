import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from difflib import SequenceMatcher
from hashlib import sha1

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
_TRANSLATION_RACE_EXECUTOR = ThreadPoolExecutor(max_workers=24, thread_name_prefix="translate-race")


def voice_for_lang(lang):
    return LANG_VOICES.get(lang, DEFAULT_VOICE_ID)


TRANSLATE_SYSTEM_WITH_ROSTER = """Translate the English football commentary to {lang_name}.
Fix any misspelled player/team/venue names using the roster below, then translate.

PLAYER ROSTER:
{roster}

Rules:
1. Translate the MEANING faithfully. Render English football idioms naturally in the target language.
2. Fix misspelled names from the roster, keep all other names unchanged.
3. Do NOT invent details, actions, players, events, score state, or tactical context that are not stated in the source.
4. Return ONLY the translation. Never answer the input, explain, apologize, or refuse.
5. Match the length and fragment structure of the original. If the source is a fragment, keep it as a fragment.
6. Ordinary short English phrases are safe to translate. Use __TRANSLATION_FAILED__ only for truly impossible or non-language input.
7. Use correct grammar — never invent word forms.

Additional guidance:
- English football commentary contains idioms and figurative expressions. Render the intended sporting meaning as natural target-language commentary; do not preserve literal source wording when it sounds unnatural.
- Short fragments of 1-3 words are typically sentence continuations, not standalone instructions. Translate them as fragments without elaborating.
- For ambiguous bare verb fragments, use a neutral fragment form rather than adding tense, polarity, subject, or intent."""

TRANSLATE_SYSTEM = """Translate the English football commentary to {lang_name}.
Rules:
1. Translate the MEANING faithfully. Render English football idioms naturally in the target language.
2. Keep player names, team names, and proper nouns unchanged.
3. Do NOT invent details, actions, players, events, score state, or tactical context that are not stated in the source.
4. Return ONLY the translation. Never answer the input, explain, apologize, or refuse.
5. Match the length and fragment structure of the original. If the source is a fragment, keep it as a fragment.
6. Ordinary short English phrases are safe to translate. Use __TRANSLATION_FAILED__ only for truly impossible or non-language input.
7. Use correct grammar — never invent word forms.

Additional guidance:
- English football commentary contains idioms and figurative expressions. Render the intended sporting meaning as natural target-language commentary; do not preserve literal source wording when it sounds unnatural.
- Short fragments of 1-3 words are typically sentence continuations, not standalone instructions. Translate them as fragments without elaborating.
- For ambiguous bare verb fragments, use a neutral fragment form rather than adding tense, polarity, subject, or intent."""

_REFUSAL_PATTERNS = [
    "i'm sorry", "i am sorry", "i cannot help", "i can't help",
    "cannot assist", "can't assist", "as an ai",
    "je suis désolé", "je suis desole", "je ne peux pas", "je ne peux vous",
    "lo siento", "no puedo ayudar", "no puedo asistir",
    "desculpe", "não posso ajudar", "nao posso ajudar",
    "tut mir leid", "ich kann nicht helfen",
    "üzgünüm", "yardımcı olamam",
]

# Temporary tripwire for current fallback over-literalising common English
# football idioms. Do not grow this into a phrase dictionary; remove when
# Phase 2 selects a fallback with a lower guard-rejection rate.
_FALLBACK_IDIOM_TRIPWIRE = [
    (
        "every day of the week",
        [
            "tous les jours de la semaine",
            "todos los días de la semana",
            "todos os dias da semana",
            "jeden tag der woche",
            "haftanın her günü",
        ],
    ),
]


def _translation_preview(text, limit=120):
    text = (text or "").replace("\n", " ").strip()
    return text[:limit]


def _attempt_public(attempt):
    public = dict(attempt)
    output = public.pop("output", "")
    public["output_preview"] = _translation_preview(output)
    public["output_sha1"] = sha1(output.encode("utf-8")).hexdigest()[:12] if output else ""
    return public


def guard_translation_output(source, translated, lang):
    """Return (ok, reason) for a translation candidate."""
    out = (translated or "").strip()
    src = (source or "").strip()
    if not out:
        return False, "empty"
    if out == "__TRANSLATION_FAILED__":
        return False, "sentinel"

    low = out.lower()
    for pattern in _REFUSAL_PATTERNS:
        if pattern in low:
            return False, "assistant_refusal"
    src_low = src.lower()
    for source_idiom, literal_outputs in _FALLBACK_IDIOM_TRIPWIRE:
        if source_idiom in src_low and any(p in low for p in literal_outputs):
            return False, "literal_idiom"

    # Guard obvious meta-output. Legitimate commentary should not include labels.
    if low.startswith(("translation:", "traduction:", "output:", "réponse:", "reponse:")):
        return False, "meta_output"

    src_len = max(1, len(src))
    ratio = len(out) / src_len
    if src_len <= 12 and ratio > 3.0:
        return False, f"short_length_ratio_{ratio:.2f}"
    if ratio < 0.25 or ratio > 4.0:
        return False, f"length_ratio_{ratio:.2f}"

    return True, "ok"


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


def _build_user_message(text, previous_source=None, previous_translation=None):
    if not previous_source and not previous_translation:
        return text
    parts = [
        "Use this previous utterance only as context for pronouns, fragments, and sentence continuations.",
        "Do not translate the previous utterance again.",
    ]
    if previous_source:
        parts.append(f"Previous English: {previous_source}")
    if previous_translation:
        parts.append(f"Previous target translation: {previous_translation}")
    parts.append(f"Current English to translate: {text}")
    return "\n".join(parts)


def translate_text(oai_client, text, lang, model="gpt-5.5",
                    reasoning_effort="low", roster=None,
                    previous_source=None, previous_translation=None):
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
            {"role": "user", "content": _build_user_message(
                text, previous_source=previous_source,
                previous_translation=previous_translation)},
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


def translate_text_with_fallback(oai_client, text, lang, model="gpt-5.5",
                                 reasoning_effort="low", roster=None,
                                 fallback_model="gpt-5.4",
                                 fallback_reasoning_effort=None,
                                 previous_source=None,
                                 previous_translation=None,
                                 primary_grace_s=1.5,
                                 guard_primary_wait_s=3.0,
                                 return_meta=False):
    """Race the preferred translation model against a fast fallback.

    The preferred model wins if it finishes within primary_grace_s. After that,
    whichever model returns first is used. The losing request is not cancelled;
    OpenAI calls are already in flight and may still be billed.
    """
    if not fallback_model or fallback_model == model:
        attempt = _translate_attempt(
            oai_client, text, lang, model, reasoning_effort, roster,
            previous_source=previous_source,
            previous_translation=previous_translation)
        selected = attempt if attempt.get("guard_ok") else None
        translated = selected.get("output", "") if selected else ""
        reason = "primary_only" if selected else f"guard_rejected_{attempt.get('guard_reason')}"
        if return_meta:
            return translated, model, reason, {
                "selected_model": model if selected else None,
                "selected_reason": reason,
                "guard_status": "accepted" if selected else "rejected",
                "guard_reason": attempt.get("guard_reason"),
                "attempts": [_attempt_public(attempt)],
            }
        return translated, model, reason

    started = time.monotonic()
    primary = _TRANSLATION_RACE_EXECUTOR.submit(
        _translate_attempt, oai_client, text, lang, model, reasoning_effort, roster,
        previous_source, previous_translation)
    fallback_effort = fallback_reasoning_effort
    if fallback_effort is None and _is_reasoning_model(fallback_model):
        fallback_effort = "low"
    fallback = _TRANSLATION_RACE_EXECUTOR.submit(
        _translate_attempt, oai_client, text, lang, fallback_model, fallback_effort, roster,
        previous_source, previous_translation)

    attempts = {}

    def _collect(name, fut):
        if name in attempts:
            return attempts[name]
        try:
            attempts[name] = fut.result()
        except Exception as exc:
            attempts[name] = {
                "model": model if name == "primary" else fallback_model,
                "role": name,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "guard_ok": False,
                "guard_reason": "exception",
            }
        attempts[name]["role"] = name
        return attempts[name]

    def _pending_attempt(name):
        return {
            "model": model if name == "primary" else fallback_model,
            "role": name,
            "ok": None,
            "guard_ok": None,
            "guard_reason": "pending_at_selection",
        }

    def _finish(selected, fallback_reason, guard_status="accepted"):
        public_attempts = []
        for name, fut in (("primary", primary), ("fallback", fallback)):
            if fut.done():
                public_attempts.append(_attempt_public(_collect(name, fut)))
            else:
                public_attempts.append(_pending_attempt(name))
        translated = selected.get("output", "") if selected else ""
        model_used = selected.get("model") if selected else model
        if return_meta:
            return translated, model_used, fallback_reason, {
                "selected_model": selected.get("model") if selected else None,
                "selected_reason": fallback_reason,
                "guard_status": guard_status,
                "guard_reason": selected.get("guard_reason") if selected else fallback_reason,
                "attempts": public_attempts,
            }
        return translated, model_used, fallback_reason

    done, _pending = wait({primary}, timeout=primary_grace_s)
    if primary in done:
        attempt = _collect("primary", primary)
        if attempt.get("guard_ok"):
            return _finish(attempt, "primary_fast")
        if fallback.done():
            fallback_attempt = _collect("fallback", fallback)
        else:
            fallback_attempt = _collect("fallback", fallback)
        if fallback_attempt.get("guard_ok"):
            return _finish(fallback_attempt, "fallback_after_primary_rejected")
        return _finish(None, f"guard_rejected_{attempt.get('guard_reason')}", "rejected")

    pending = {primary, fallback}
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        if fallback in done:
            fallback_attempt = _collect("fallback", fallback)
            if fallback_attempt.get("guard_ok"):
                return _finish(fallback_attempt, f"fallback_after_{primary_grace_s:.1f}s")
            # Suspicious fallback output must not reach TTS. Give the primary
            # a short extra chance before dropping the utterance.
            wait({primary}, timeout=guard_primary_wait_s)
            if primary.done():
                primary_attempt = _collect("primary", primary)
                if primary_attempt.get("guard_ok"):
                    elapsed = time.monotonic() - started
                    return _finish(primary_attempt, f"primary_after_fallback_rejected_{elapsed:.1f}s")
            return _finish(None, f"guard_rejected_{fallback_attempt.get('guard_reason')}", "rejected")
        if primary in done:
            primary_attempt = _collect("primary", primary)
            if primary_attempt.get("guard_ok"):
                return _finish(primary_attempt, f"primary_after_{time.monotonic() - started:.1f}s")
            if fallback.done():
                fallback_attempt = _collect("fallback", fallback)
                if fallback_attempt.get("guard_ok"):
                    return _finish(fallback_attempt, "fallback_after_primary_rejected")
            return _finish(None, f"guard_rejected_{primary_attempt.get('guard_reason')}", "rejected")

    return _finish(None, "primary_fallback_unavailable", "rejected")


def _translate_attempt(oai_client, text, lang, model, reasoning_effort, roster,
                       previous_source=None, previous_translation=None):
    started_at = time.time()
    started = time.monotonic()
    attempt = {
        "model": model,
        "started_at": started_at,
        "ok": False,
    }
    try:
        output = translate_text(
            oai_client, text, lang, model=model,
            reasoning_effort=reasoning_effort, roster=roster,
            previous_source=previous_source,
            previous_translation=previous_translation)
        ended_at = time.time()
        ok, reason = guard_translation_output(text, output, lang)
        attempt.update({
            "ok": True,
            "output": output,
            "ended_at": ended_at,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "guard_ok": ok,
            "guard_reason": reason,
        })
    except Exception as exc:
        attempt.update({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "ended_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "guard_ok": False,
            "guard_reason": "exception",
        })
    return attempt


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
                       model="gpt-5.5", reasoning_effort="low"):
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

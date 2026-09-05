#!/usr/bin/env python3
"""Transcribe rendered AI tracks and compare the actual speech with the script."""
from __future__ import annotations

import difflib
import json
import re
import unicodedata

import requests

from tennis_common import (
    ARTIFACTS,
    CONFIG,
    assert_football_idle,
    load_env,
    read_jsonl,
    require_env,
)

LANGUAGES = {"en": "en", "fr": "fr", "pt": "pt-BR"}
NUMBERS = {
    "en": {
        "0": "zero", "1": "one", "2": "two", "3": "three",
        "15": "fifteen", "30": "thirty", "40": "forty",
    },
    "fr": {
        "0": "zero", "1": "un", "2": "deux", "3": "trois",
        "15": "quinze", "30": "trente", "40": "quarante",
    },
    "pt": {
        "0": "zero", "1": "um", "2": "dois", "3": "tres",
        "15": "quinze", "30": "trinta", "40": "quarenta",
    },
}


def normalized(text: str, lang: str) -> list[str]:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
    value = re.sub(r"\b1(?:er|o)?\b", "premier" if lang == "fr" else
                   ("primeiro" if lang == "pt" else "first"), value)
    for digits, spoken in sorted(
        NUMBERS[lang].items(), key=lambda item: -len(item[0])
    ):
        value = re.sub(rf"\b{digits}\b", spoken, value)
    return re.findall(r"\b[\w']+\b", value)


def similarity(expected: str, actual: str, lang: str) -> float:
    return difflib.SequenceMatcher(
        None,
        normalized(expected, lang),
        normalized(actual, lang),
    ).ratio()


def transcribe(path, language: str) -> dict:
    assert_football_idle()
    response = requests.post(
        "https://api.deepgram.com/v1/listen",
        params={
            "model": CONFIG["models"]["stt"],
            "language": language,
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
        },
        headers={
            "Authorization": (
                f"Token {__import__('os').environ['DEEPGRAM_API_KEY']}"
            ),
            "Content-Type": "audio/wav",
        },
        data=path.read_bytes(),
        timeout=360,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["DEEPGRAM_API_KEY"])
    rows = [
        row
        for row in read_jsonl(ARTIFACTS / "commentary_attempt_1.jsonl")
        if not row.get("dropped")
    ]
    reports = []
    faults = []
    for lang, provider_language in LANGUAGES.items():
        path = ARTIFACTS / f"ai_{lang}.wav"
        if not path.exists():
            raise SystemExit(f"missing rendered track: {path}")
        payload = transcribe(path, provider_language)
        channel = (
            payload.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
        )
        transcript = str(channel.get("transcript") or "").strip()
        words = channel.get("words") or []
        expected = " ".join(row[{"en": "text", "fr": "fr", "pt": "pt"}[lang]] for row in rows)
        aggregate = similarity(expected, transcript, lang)
        line_reports = []
        for row in rows:
            placement = row["placements"][lang]
            start = float(placement["start_s"]) - 0.35
            end = float(placement["end_s"]) + 0.35
            actual_words = [
                str(word.get("punctuated_word") or word.get("word") or "")
                for word in words
                if start
                <= (float(word.get("start", 0)) + float(word.get("end", 0))) / 2
                <= end
            ]
            actual = " ".join(actual_words)
            expected_line = row[{"en": "text", "fr": "fr", "pt": "pt"}[lang]]
            score = similarity(expected_line, actual, lang)
            line_reports.append(
                {
                    "video_time_s": row["video_time_s"],
                    "expected": expected_line,
                    "heard": actual,
                    "similarity": round(score, 4),
                }
            )
            if score < 0.55:
                faults.append(
                    f"{lang} speech mismatch at {row['video_time_s']}s: "
                    f"{score:.3f}"
                )
        first_word = float(words[0]["start"]) if words else None
        last_word = float(words[-1]["end"]) if words else None
        if aggregate < 0.78:
            faults.append(f"{lang} aggregate speech similarity {aggregate:.3f}")
        if first_word is None or first_word > 4.0:
            faults.append(f"{lang} opener was not heard near track start")
        if last_word is None or last_word < 291.0:
            faults.append(f"{lang} final outcome was not heard near track end")
        reports.append(
            {
                "language": lang,
                "aggregate_similarity": round(aggregate, 4),
                "first_word_s": first_word,
                "last_word_s": last_word,
                "transcript": transcript,
                "lines": line_reports,
            }
        )
    result = {
        "version": CONFIG["version"],
        "profile": __import__("os").environ.get("TENNIS_PROFILE", "10s"),
        "status": "PASS" if not faults else "FAIL",
        "method": "independent STT of the rendered AI-only WAV tracks",
        "reports": reports,
        "faults": faults,
    }
    out = ARTIFACTS / "render_audit.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                "status": result["status"],
                "profile": result["profile"],
                "languages": [
                    {
                        "language": report["language"],
                        "aggregate_similarity": report["aggregate_similarity"],
                        "minimum_line_similarity": min(
                            line["similarity"] for line in report["lines"]
                        ),
                        "first_word_s": report["first_word_s"],
                        "last_word_s": report["last_word_s"],
                    }
                    for report in reports
                ],
                "faults": faults,
            },
            indent=2,
        )
    )
    if faults:
        raise SystemExit("rendered-track audit failed")


if __name__ == "__main__":
    main()

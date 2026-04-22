#!/usr/bin/env python3
"""
Live Soccer Commentary → Agora Avatar Feeder

Polls Sportradar Extended API for live match commentary, AI insights,
and fun facts, then pushes them to an Agora Conversational AI agent
via simple-backend's /speak endpoint.

Uses the Soccer Extended API for richer AI-generated content:
  - timeline.json: play-by-play commentary
  - insights.json: AI pre-match and live insights
  - fun_facts.json: AI-generated stat nuggets

Usage:
    # Live match (agent_id from the frontend):
    python3 commentary_feeder.py --agent-id A42AP79CD4... sr:sport_event:69339340

    # Replay saved commentary file:
    python3 commentary_feeder.py --agent-id A42AP79CD4... --replay brighton_vs_liverpool_commentary.txt

    # Custom poll interval:
    python3 commentary_feeder.py --agent-id A42AP79CD4... sr:sport_event:69339340 --interval 3
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# Sportradar Extended API
SPORTRADAR_API_KEY = os.environ.get("SPORTRADAR_API_KEY", "")
SPORTRADAR_BASE_URL = "https://api.sportradar.com/soccer-extended/trial/v4/en"

# Backend settings
BACKEND_URL = "http://localhost:8082"

# ─── Translation ─────────────────────────────────────────────────────────

LANG_NAMES = {
    "en": "English",
    "es": "Spanish (Latin American)",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese (Brazilian)",
    "it": "Italian",
    "ar": "Arabic",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Mandarin Chinese",
    "hi": "Hindi",
}

TRANSLATE_SYSTEM = """You are a real-time translator for live soccer commentary.
Translate the English soccer commentary to {lang_name}. Rules:
1. Keep player names, team names, and proper nouns unchanged
2. Maintain the energy and rhythm of live commentary — this will be spoken aloud by TTS
3. Use natural soccer terminology for the target language
4. Return ONLY the translation, no explanations
5. Keep it concise — match the length of the original"""

_oai_client = None


def get_translator(lang):
    """Return a translate function if lang != 'en', else None."""
    if not lang or lang == "en":
        return None

    import openai
    global _oai_client
    if _oai_client is None:
        _oai_client = openai.OpenAI()

    lang_name = LANG_NAMES.get(lang, lang)
    sys_prompt = TRANSLATE_SYSTEM.format(lang_name=lang_name)

    def translate(text):
        resp = _oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=512,
        )
        return resp.choices[0].message.content.strip()

    return translate

# Event types that should interrupt current speech (high-priority)
INTERRUPT_EVENTS = {
    "score_change", "red_card", "yellow_red_card",
    "penalty_awarded", "penalty_missed", "penalty_shootout",
}


def sportradar_get(path):
    """Fetch JSON from the Sportradar Extended API."""
    url = f"{SPORTRADAR_BASE_URL}/{path}"
    req = urllib.request.Request(url, headers={"x-api-key": SPORTRADAR_API_KEY})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def speak(backend_url, agent_id, text, priority="APPEND"):
    """Push text to the agent's TTS via simple-backend /speak endpoint."""
    url = f"{backend_url}/speak"
    payload = json.dumps({
        "agent_id": agent_id,
        "text": text,
        "priority": priority,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"  [speak] WARNING: {e.code} — {body}")
        return e.code, body


def classify_priority(event_type):
    """Goals, red cards, and penalties interrupt; everything else appends."""
    if event_type in INTERRUPT_EVENTS:
        return "INTERRUPT"
    return "APPEND"


def extract_commentary_text(event):
    """Extract plain commentary text from a Sportradar timeline event."""
    texts = []
    for c in event.get("commentaries", []):
        text = c.get("text", "").strip()
        if text:
            texts.append(text)
    return texts


def fetch_insights(sport_event_id):
    """Fetch AI-generated insights (pre-match and live)."""
    try:
        data = sportradar_get(f"sport_events/{sport_event_id}/insights.json")
        return data.get("insights", [])
    except Exception as e:
        print(f"  [insights] {e}")
        return []


def fetch_fun_facts(sport_event_id):
    """Fetch AI-generated fun facts."""
    try:
        data = sportradar_get(f"sport_events/{sport_event_id}/fun_facts.json")
        return data.get("fun_facts", [])
    except Exception as e:
        print(f"  [fun_facts] {e}")
        return []


def maybe_translate(text, translate_fn):
    """Translate text if a translate function is provided."""
    if translate_fn:
        try:
            return translate_fn(text)
        except Exception as e:
            print(f"  [translate ERROR] {e}")
    return text


def deliver_prematch(sport_event_id, agent_id, backend_url, translate_fn=None):
    """
    Deliver pre-match insights and fun facts before kickoff.
    Sorted by relevancy so the most interesting ones come first.
    """
    print(f"\n  --- PRE-MATCH INSIGHTS ---")

    insights = fetch_insights(sport_event_id)
    prematch = [i for i in insights if i.get("type") == "prematch"]
    prematch.sort(key=lambda x: x.get("relevancy", 0), reverse=True)

    if prematch:
        # Deliver top 3 most relevant insights
        for i in prematch[:3]:
            text = i.get("text", "")
            rel = i.get("relevancy", 0)
            print(f"  [insight] (rel={rel:.1f}) {text}")
            speak(backend_url, agent_id, maybe_translate(text, translate_fn), "APPEND")
            time.sleep(1)
    else:
        print(f"  [insights] No pre-match insights available")

    # Fun facts
    time.sleep(1)
    facts = fetch_fun_facts(sport_event_id)
    if facts:
        for f in facts[:2]:
            text = f.get("text", "")
            print(f"  [fun_fact] {text}")
            speak(backend_url, agent_id, maybe_translate(text, translate_fn), "APPEND")
            time.sleep(1)

    print(f"  --- END PRE-MATCH ---\n")


def feed_match(sport_event_id, agent_id, backend_url, interval=5,
               translate_fn=None):
    """Main loop: poll Sportradar Extended API for commentary and push to the avatar."""
    seen_count = 0
    seen_insight_texts = set()
    prematch_delivered = False
    last_insight_check = 0
    insight_check_interval = 60  # check for new live insights every 60s

    print(f"\n{'=' * 60}")
    print(f"  FEEDING COMMENTARY (Extended API): {sport_event_id}")
    print(f"  Agent: {agent_id}")
    print(f"  Polling every {interval}s — Ctrl+C to stop")
    print(f"{'=' * 60}\n")

    while True:
        try:
            data = sportradar_get(f"sport_events/{sport_event_id}/timeline.json")
        except Exception as e:
            print(f"  [poll] Error: {e}")
            time.sleep(interval)
            continue

        status = data.get("sport_event_status", {})
        match_status = status.get("status", "not_started")
        home = status.get("home_score", 0)
        away = status.get("away_score", 0)

        # Deliver pre-match insights once while waiting for kickoff
        if match_status == "not_started" and not prematch_delivered:
            deliver_prematch(sport_event_id, agent_id, backend_url, translate_fn)
            prematch_delivered = True

        timeline = data.get("timeline", [])
        commentary_events = [e for e in timeline if e.get("commentaries")]

        # Push new commentary to the avatar — always INTERRUPT to avoid delay buildup
        if len(commentary_events) > seen_count:
            new_events = commentary_events[seen_count:]
            for event in new_events:
                event_type = event.get("type", "")
                match_time = event.get("match_time", "")
                stoppage = event.get("stoppage_time", "")
                time_str = f"{match_time}'+{stoppage}" if stoppage else f"{match_time}'" if match_time else ""

                texts = extract_commentary_text(event)
                for text in texts:
                    speak_text = maybe_translate(text, translate_fn)
                    print(f"  [{time_str:>7}] [INTERRUPT] {speak_text}")
                    speak(backend_url, agent_id, speak_text, "INTERRUPT")
                    time.sleep(0.3)

            seen_count = len(commentary_events)

            # Announce score after goals
            if any(e.get("type") == "score_change" for e in new_events):
                score_text = f"The score is now {home} to {away}."
                score_text = maybe_translate(score_text, translate_fn)
                print(f"           [INTERRUPT] {score_text}")
                speak(backend_url, agent_id, score_text, "INTERRUPT")

        # Periodically check for new live insights during the match
        now = time.time()
        if match_status == "live" and (now - last_insight_check) > insight_check_interval:
            last_insight_check = now
            try:
                insights = fetch_insights(sport_event_id)
                live_insights = [i for i in insights if i.get("type") == "live"]
                for i in live_insights:
                    text = i.get("text", "")
                    if text and text not in seen_insight_texts:
                        seen_insight_texts.add(text)
                        rel = i.get("relevancy", 0)
                        speak_text = maybe_translate(text, translate_fn)
                        print(f"  [INSIGHT] (rel={rel:.1f}) {speak_text}")
                        speak(backend_url, agent_id, speak_text, "INTERRUPT")
                        time.sleep(0.5)
            except Exception as e:
                print(f"  [insights] {e}")

        if match_status in ("closed", "ended"):
            print(f"\n{'=' * 60}")
            print(f"  FULL TIME: {home}-{away}")
            print(f"{'=' * 60}")
            ft_text = f"Full time! The final score is {home} to {away}."
            ft_text = maybe_translate(ft_text, translate_fn)
            speak(backend_url, agent_id, ft_text, "INTERRUPT")
            break

        if match_status == "not_started":
            sys.stdout.write(f"\r  Waiting for kickoff... ({time.strftime('%H:%M:%S')})")
            sys.stdout.flush()

        time.sleep(interval)


def replay_file(filepath, agent_id, backend_url, delay=3.0, translate_fn=None):
    """Replay a saved commentary file line by line through the avatar."""
    line_re = re.compile(r'^\s*\[.*?\]\s*(.+)$')

    print(f"\n{'=' * 60}")
    print(f"  REPLAYING: {filepath}")
    print(f"  Agent: {agent_id}")
    print(f"  Delay between lines: {delay}s")
    print(f"{'=' * 60}\n")

    with open(filepath, "r") as f:
        lines = f.readlines()

    for line in lines:
        line = line.rstrip()
        if not line or line.startswith("=") or line.startswith("---"):
            continue

        # Skip header lines
        if any(line.strip().startswith(prefix) for prefix in [
            "PREMIER LEAGUE", "Brighton", "Liverpool", "American Express",
            "Referee:", ">> ", "FULL TIME"
        ]):
            if line.strip().startswith(">>") or "FULL TIME" in line:
                text = line.strip().lstrip("> ").strip()
                if text:
                    speak_text = maybe_translate(text, translate_fn)
                    print(f"  [SPEAK] {speak_text}")
                    speak(backend_url, agent_id, speak_text, "INTERRUPT")
                    time.sleep(delay)
            continue

        # Extract commentary text from formatted lines
        match = line_re.match(line)
        if match:
            text = match.group(1).strip()
            text = re.sub(r'\033\[[0-9;]*m', '', text)
            text = text.strip()
        else:
            text = line.strip()

        if not text:
            continue

        speak_text = maybe_translate(text, translate_fn)
        print(f"  [INTERRUPT] {speak_text}")
        speak(backend_url, agent_id, speak_text, "INTERRUPT")
        time.sleep(delay)

    print(f"\n{'=' * 60}")
    print(f"  REPLAY COMPLETE")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="Feed live Sportradar commentary to an Agora avatar agent"
    )
    parser.add_argument(
        "match_id", nargs="?",
        help="Sport event ID (e.g. sr:sport_event:69339340)"
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent ID from the frontend (shown in settings after connecting)"
    )
    parser.add_argument(
        "--replay", metavar="FILE",
        help="Replay a saved commentary file instead of polling live"
    )
    parser.add_argument(
        "--interval", type=int, default=5,
        help="Poll interval in seconds (default: 5)"
    )
    parser.add_argument(
        "--delay", type=float, default=3.0,
        help="Delay between lines in replay mode (default: 3.0s)"
    )
    parser.add_argument(
        "--backend", default=BACKEND_URL,
        help=f"Backend URL (default: {BACKEND_URL})"
    )
    parser.add_argument(
        "--lang", default="en",
        help="Output language (en, es, fr, de, pt, etc.)"
    )
    args = parser.parse_args()

    if not args.match_id and not args.replay:
        parser.error("Provide a match_id or use --replay FILE")

    agent_id = args.agent_id
    translate_fn = get_translator(args.lang)

    print(f"  Using agent: {agent_id}")
    print(f"  Backend: {args.backend}")
    print(f"  API: Sportradar Soccer Extended v4")
    if translate_fn:
        print(f"  Translation: English → {LANG_NAMES.get(args.lang, args.lang)}")

    try:
        if args.replay:
            replay_file(args.replay, agent_id, args.backend, args.delay, translate_fn)
        else:
            feed_match(
                args.match_id,
                agent_id=agent_id,
                backend_url=args.backend,
                interval=args.interval,
                translate_fn=translate_fn,
            )
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user.")


if __name__ == "__main__":
    main()

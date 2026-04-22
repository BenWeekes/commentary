#!/usr/bin/env python3
"""
Match Replay — plays back a pre-generated events file through the avatar.

Reads an events file (offset_seconds|priority|message) and sends each
message to the avatar via /speak at the correct time offset.

Supports optional translation to any language via GPT-4o-mini.

Usage:
    python3 match_replay.py --agent-id ABC123 --events replay_39_45.txt
    python3 match_replay.py --agent-id ABC123 --events replay_39_45.txt --lang es
    python3 match_replay.py --agent-id ABC123 --events replay_39_45.txt --speed 3
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


BACKEND_URL = "http://localhost:8082"
LIVE_LOG = "sportradar.live"

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


def speak(backend_url, agent_id, text, priority="APPEND"):
    """Push text to the avatar's TTS via /speak endpoint."""
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
            return resp.status
    except urllib.error.HTTPError as e:
        print(f"  [speak ERROR] {e.code} - {e.read().decode()}")
        return e.code


def load_events(filepath):
    """Load events from file. Returns list of (offset_secs, priority, message)."""
    events = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            # Support both "mm:ss" and plain seconds format
            ts = parts[0]
            if ':' in ts:
                mm, ss = ts.split(':')
                offset = int(mm) * 60 + int(ss)
            else:
                offset = int(ts)
            priority = parts[1]
            message = parts[2]
            events.append((offset, priority, message))
    return events


def replay(events, agent_id, backend_url, speed, match_minute=38, match_second=0,
           translate_fn=None, lang="en"):
    """Replay events at the correct time offsets."""
    if not events:
        print("No events to replay.")
        return

    total_duration = events[-1][0]
    real_duration = total_duration / speed

    # Clear and open live log file
    live_log = open(LIVE_LOG, "w")
    live_log.flush()

    lang_label = f" → {LANG_NAMES.get(lang, lang)}" if lang != "en" else ""

    print(f"\n{'=' * 60}")
    print(f"  MATCH REPLAY{lang_label}")
    print(f"  Events: {len(events)}")
    print(f"  Match time span: {total_duration}s ({total_duration // 60}m {total_duration % 60}s)")
    print(f"  Speed: {speed}x -> real duration: {real_duration:.0f}s ({real_duration / 60:.1f}m)")
    print(f"  Live log: tail -f {LIVE_LOG}")
    print(f"{'=' * 60}\n")

    wall_start = time.time()
    delivered = 0

    for offset, priority, message in events:
        # When should this event fire in real wall-clock time?
        target_wall = wall_start + (offset / speed)

        # Wait until it's time
        now = time.time()
        wait = target_wall - now
        if wait > 0:
            time.sleep(wait)

        # Translate if needed
        speak_text = message
        if translate_fn:
            try:
                speak_text = translate_fn(message)
            except Exception as e:
                print(f"  [translate ERROR] {e}")

        # Deliver
        elapsed_real = time.time() - wall_start
        total_secs = match_minute * 60 + match_second + offset
        mm = total_secs // 60
        ss = total_secs % 60

        # Write to live log file
        tag = ">>>" if priority == "INTERRUPT" else "   "
        log_line = f"{tag} [{mm:02d}:{ss:02d}] {speak_text}"
        live_log.write(log_line + "\n")
        live_log.flush()

        status = speak(backend_url, agent_id, speak_text, priority)
        delivered += 1

        if status != 200:
            live_log.write(f"    [ERROR {status}]\n")
            live_log.flush()

    elapsed = time.time() - wall_start
    live_log.close()

    print(f"\n{'=' * 60}")
    print(f"  REPLAY COMPLETE")
    print(f"  Delivered: {delivered}/{len(events)} events in {elapsed:.1f}s")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(description="Replay match events through avatar")
    parser.add_argument("--agent-id", required=True, help="Agent ID from frontend")
    parser.add_argument("--events", required=True, help="Path to events file")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Playback speed (1.0=real-time, 2.0=2x fast, 0.5=half speed)")
    parser.add_argument("--backend", default=BACKEND_URL, help="Backend URL")
    parser.add_argument("--match-minute", type=int, default=38,
                        help="Match minute to display in log timestamps (default: 38)")
    parser.add_argument("--match-second", type=int, default=0,
                        help="Match second offset to display in log timestamps (default: 0)")
    parser.add_argument("--lang", default="en",
                        help="Output language (en, es, fr, de, pt, etc.)")
    args = parser.parse_args()

    print(f"Loading events from {args.events}...")
    events = load_events(args.events)
    print(f"Loaded {len(events)} events.")

    translate_fn = get_translator(args.lang)
    if translate_fn:
        print(f"Translation enabled: English → {LANG_NAMES.get(args.lang, args.lang)}")

    try:
        replay(events, args.agent_id, args.backend, args.speed,
               args.match_minute, args.match_second,
               translate_fn=translate_fn, lang=args.lang)
    except KeyboardInterrupt:
        print("\n\nReplay stopped.")


if __name__ == "__main__":
    main()

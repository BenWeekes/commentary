#!/usr/bin/env python3
"""
Generate a timestamped multilingual transcript of the 5-min demo clip.

Runs Deepgram STT on the audio, loads SR events, translates both into
5 languages, and outputs a formatted transcript ordered by time + language.

Output format: time : lang : SR/STT : text
Language order: en first, then alphabetical (de, es, fr, pt, tr)

Usage:
    python3 generate_demo_transcript.py              # full pipeline
    python3 generate_demo_transcript.py --stt-only   # STT + SR only (no translation)
    python3 generate_demo_transcript.py --translate   # translate from saved STT output
"""

import argparse
import json
import os
import sys
import time

# Load .env
def _load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

import urllib.request

from lib.corrections import TERMS_LIST, apply_corrections
from lib.translator import LANG_NAMES, translate_text
from lib.audio import convert_to_pcm, pcm_chunks_realtime

AUDIO_PATH = "clips/bmg_fch_demo_5min/audio.mp3"
EVENTS_PATH = "clips/bmg_fch_demo_5min/events.txt"
LANGS = ["de", "es", "fr", "pt", "tr"]  # alphabetical (en handled separately)
OUTPUT_PATH = "demo_transcript.txt"
STT_CACHE_PATH = "demo_stt_cache.json"

# Sportradar
SPORTRADAR_BASE_URL = "https://api.sportradar.com/soccer-extended/trial/v4/en"
SPORT_EVENT_ID = "sr:sport_event:61514104"  # BMG vs FCH MD28


def fetch_roster(sport_event_id):
    """Fetch player roster from Sportradar lineups endpoint.
    Returns a formatted string for the GPT prompt.
    """
    sr_key = os.environ.get("SPORTRADAR_API_KEY", "")
    if not sr_key:
        print("[ROSTER] No SPORTRADAR_API_KEY — skipping roster fetch")
        return None

    url = f"{SPORTRADAR_BASE_URL}/sport_events/{sport_event_id}/lineups.json"
    req = urllib.request.Request(url, headers={"x-api-key": sr_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[ROSTER] Failed to fetch lineups: {e}")
        return None

    lines = []
    se = data.get("sport_event", {})

    # Venue
    venue = se.get("venue", {})
    if venue.get("name"):
        lines.append(f"Venue: {venue['name']}, {venue.get('city_name', '')}")

    # Referees
    refs = se.get("sport_event_conditions", {}).get("referees", [])
    if refs:
        ref_names = [r.get("name", "") for r in refs if r.get("name")]
        lines.append(f"Referees: {', '.join(ref_names)}")

    # Teams
    lineups = data.get("lineups", {})
    for team in lineups.get("competitors", []):
        tname = team.get("name", "?")
        abbr = team.get("abbreviation", "?")
        qualifier = team.get("qualifier", "?")
        lines.append(f"\n{tname} ({abbr}) — {qualifier}:")

        mgr = team.get("manager", {})
        if mgr.get("name"):
            # SR format is "Last, First" — flip it
            parts = mgr["name"].split(", ", 1)
            mgr_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else mgr["name"]
            lines.append(f"  Manager: {mgr_name}")

        players = team.get("players", [])
        starting = [p for p in players if p.get("starter")]
        subs = [p for p in players if not p.get("starter")]

        if starting:
            lines.append("  Starting XI:")
            for p in sorted(starting, key=lambda x: x.get("jersey_number", 99)):
                lines.append(f"    #{p.get('jersey_number','?')} {p.get('name','?')}")
        if subs:
            lines.append("  Substitutes:")
            for p in sorted(subs, key=lambda x: x.get("jersey_number", 99)):
                lines.append(f"    #{p.get('jersey_number','?')} {p.get('name','?')}")

    roster = "\n".join(lines)
    player_count = sum(
        len(t.get("players", []))
        for t in lineups.get("competitors", [])
    )
    print(f"[ROSTER] Loaded {player_count} players from Sportradar lineups")
    return roster


def get_stt_utterances():
    """Run Deepgram STT on the audio file, return list of (time_s, text)."""
    import threading

    deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not deepgram_key:
        print("ERROR: DEEPGRAM_API_KEY not set")
        sys.exit(1)

    os.environ["DEEPGRAM_API_KEY"] = deepgram_key
    from deepgram import DeepgramClient
    from deepgram.listen import ListenV1Results

    pcm_path = convert_to_pcm(AUDIO_PATH)
    dg_client = DeepgramClient()

    utterances = []

    print("[STT] Streaming audio through Deepgram Nova-3...")
    print(f"[STT] Using {len(TERMS_LIST)} keyterms for boosting")

    with dg_client.listen.v1.connect(
        model="nova-3",
        language="en",
        encoding="linear16",
        sample_rate=16000,
        punctuate="true",
        smart_format="true",
        interim_results="true",
        keyterm=TERMS_LIST,
    ) as ws:

        def feed_audio():
            for chunk, _ in pcm_chunks_realtime(pcm_path):
                ws.send_media(chunk)
            ws.send_close_stream()

        audio_thread = threading.Thread(target=feed_audio, daemon=True)
        audio_thread.start()

        for msg in ws:
            if not isinstance(msg, ListenV1Results):
                continue
            if not msg.is_final:
                continue
            alt = msg.channel.alternatives[0]
            transcript = alt.transcript
            if not transcript:
                continue
            audio_start = msg.start if hasattr(msg, "start") and msg.start else 0
            utterances.append((audio_start, transcript))
            mm, ss = divmod(audio_start, 60)
            print(f"  [{int(mm):02d}:{ss:05.2f}] {transcript[:120]}")

    os.unlink(pcm_path)
    print(f"[STT] Done — {len(utterances)} utterances\n")
    return utterances


def get_sr_events():
    """Load SR events from file, return list of (time_s, text)."""
    events = []
    with open(EVENTS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            ts = parts[0]
            if ':' in ts:
                mm, ss = ts.split(':')
                offset = int(mm) * 60 + int(ss)
            else:
                offset = int(ts)
            events.append((float(offset), parts[2]))
    print(f"[SR] Loaded {len(events)} events\n")
    return events


def save_stt_cache(stt_utterances, sr_events):
    """Save STT + SR results to JSON cache for later translation."""
    data = {
        "stt": [[t, text] for t, text in stt_utterances],
        "sr": [[t, text] for t, text in sr_events],
    }
    with open(STT_CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[CACHE] Saved {len(stt_utterances)} STT + {len(sr_events)} SR entries to {STT_CACHE_PATH}")


def load_stt_cache():
    """Load STT + SR results from JSON cache."""
    with open(STT_CACHE_PATH) as f:
        data = json.load(f)
    stt = [(t, text) for t, text in data["stt"]]
    sr = [(t, text) for t, text in data["sr"]]
    print(f"[CACHE] Loaded {len(stt)} STT + {len(sr)} SR entries from {STT_CACHE_PATH}")
    return stt, sr


def translate_all(entries, oai_client, roster=None):
    """Translate list of (time, source, text) into all languages.
    Returns list of (time, lang, source, text) sorted by time then lang.
    Uses concurrent futures to translate all 5 languages in parallel per entry.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    total = len(entries)

    with ThreadPoolExecutor(max_workers=5) as pool:
        for idx, (t, source, en_text) in enumerate(entries):
            mm, ss = divmod(t, 60)
            ts = f"{int(mm):02d}:{ss:05.2f}"
            progress = f"[{idx+1}/{total}]"

            # English — keep raw STT as-is
            results.append((t, "en", source, en_text))
            print(f"  {progress} {ts} en/{source}: {en_text[:70]}")

            # Fire all 5 languages in parallel
            futures = {}
            for lang in LANGS:
                fut = pool.submit(translate_text, oai_client, en_text, lang,
                                  roster=roster)
                futures[fut] = lang

            for fut in as_completed(futures):
                lang = futures[fut]
                try:
                    translated = fut.result()
                except Exception as e:
                    translated = f"[ERROR: {e}]"
                results.append((t, lang, source, translated))
                print(f"  {progress} {ts} {lang}/{source}: {translated[:70]}")

    # Sort by time, then language order (en first, then alphabetical)
    lang_order = {"en": 0, "de": 1, "es": 2, "fr": 3, "pt": 4, "tr": 5}
    results.sort(key=lambda x: (x[0], lang_order.get(x[1], 99)))
    return results


def format_time(seconds):
    mm, ss = divmod(seconds, 60)
    return f"{int(mm):02d}:{ss:05.2f}"


def write_english_only(stt_utterances, sr_events, path):
    """Write English-only transcript (STT + SR merged by time)."""
    entries = []
    for t, text in stt_utterances:
        entries.append((t, "en", "STT", text))
    for t, text in sr_events:
        entries.append((t, "en", "SR", text))
    entries.sort(key=lambda x: x[0])

    with open(path, "w") as f:
        f.write("# Demo 5-min transcript — BMG vs FCH, match 14:47-19:47\n")
        f.write("# Format: time : lang : SR/STT : text\n")
        f.write("#\n")
        for t, lang, source, text in entries:
            ts = format_time(t)
            f.write(f"{ts} : {lang} : {source} : {text}\n")

    print(f"\nWrote {len(entries)} entries to {path}")


def main():
    parser = argparse.ArgumentParser(description="Generate demo transcript")
    parser.add_argument("--stt-only", action="store_true",
                        help="Run STT + load SR, save cache, write English-only transcript")
    parser.add_argument("--translate", action="store_true",
                        help="Load from cache and translate into all languages")
    args = parser.parse_args()

    if args.stt_only:
        # Step 1: STT
        print("=" * 60)
        print("STEP 1: Running Deepgram STT on demo audio")
        print("=" * 60)
        stt_utterances = get_stt_utterances()

        # Step 2: SR events
        print("=" * 60)
        print("STEP 2: Loading SR events")
        print("=" * 60)
        sr_events = get_sr_events()

        # Save cache
        save_stt_cache(stt_utterances, sr_events)

        # Write English-only output
        en_path = OUTPUT_PATH.replace(".txt", "_en.txt")
        write_english_only(stt_utterances, sr_events, en_path)
        return

    if args.translate:
        if not os.path.exists(STT_CACHE_PATH):
            print(f"ERROR: {STT_CACHE_PATH} not found. Run --stt-only first.")
            sys.exit(1)
        stt_utterances, sr_events = load_stt_cache()
    else:
        # Full pipeline
        print("=" * 60)
        print("STEP 1: Running Deepgram STT on demo audio")
        print("=" * 60)
        stt_utterances = get_stt_utterances()

        print("=" * 60)
        print("STEP 2: Loading SR events")
        print("=" * 60)
        sr_events = get_sr_events()

        save_stt_cache(stt_utterances, sr_events)

    # Fetch roster for GPT correction + translation
    print("=" * 60)
    print("Fetching player roster from Sportradar")
    print("=" * 60)
    roster = fetch_roster(SPORT_EVENT_ID)

    # Translate
    import openai
    oai_client = openai.OpenAI()

    print("=" * 60)
    print("Translating into 5 languages (gpt-5.4-mini + roster correction)")
    print("=" * 60)
    entries = []
    for t, text in stt_utterances:
        entries.append((t, "STT", text))  # raw STT — GPT corrects with roster
    for t, text in sr_events:
        entries.append((t, "SR", text))
    entries.sort(key=lambda x: x[0])

    results = translate_all(entries, oai_client, roster=roster)

    # Write output
    print("\n" + "=" * 60)
    print(f"Writing {OUTPUT_PATH}")
    print("=" * 60)

    with open(OUTPUT_PATH, "w") as f:
        f.write("# Demo 5-min transcript — BMG vs FCH, match 14:47-19:47\n")
        f.write("# Format: time : lang : SR/STT : text\n")
        f.write("# Languages: en (English), de (German), es (Spanish), "
                "fr (French), pt (Portuguese), tr (Turkish)\n")
        f.write("#\n")
        for t, lang, source, text in results:
            ts = format_time(t)
            f.write(f"{ts} : {lang} : {source} : {text}\n")

    print(f"\nDone — {len(results)} lines written to {OUTPUT_PATH}")
    print(f"  {len(stt_utterances)} STT utterances × 6 langs = {len(stt_utterances) * 6}")
    print(f"  {len(sr_events)} SR events × 6 langs = {len(sr_events) * 6}")
    print(f"  Total: {len(results)} lines")


if __name__ == "__main__":
    main()

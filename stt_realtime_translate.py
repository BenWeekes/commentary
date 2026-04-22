#!/usr/bin/env python3
"""
Real-Time STT → Correct → Translate Pipeline

Streams audio at real-time pace through Deepgram, applies deterministic
corrections, translates each utterance via GPT-4o-mini, and measures
end-to-end latency from audio timestamp to translation-ready.

Pipeline:
  Audio (real-time) → Deepgram STT → Corrections → GPT-4o-mini → Spanish
                       └─ ~0.8s ─┘    └─ <1ms ─┘   └─ ~0.8s ─┘
  Total: audio_time → TTS-ready in ~1.7s

Usage:
    source soniox_venv/bin/activate
    export OPENAI_API_KEY=...
    python3 stt_realtime_translate.py --audio bmg_fch_first_5min.mp3
    python3 stt_realtime_translate.py --audio bmg_fch_first_5min.mp3 --lang fr
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

import openai

# ─── Enhanced keyword terms for Deepgram ─────────────────────────────────

TERMS_LIST = [
    "Borussia Monchengladbach", "Heidenheim", "Gladbach", "BMG", "FCH",
    "Bundesliga", "Borussia-Park", "Monchengladbach", "Fohlenelf",
    "Matchday 28",
    "Franck Honorat", "Wael Mohya", "Jens Castrop", "Shuto Machino",
    "Nico Elvedi", "Moritz Nicolas", "Kevin Diks", "Philipp Sander",
    "Yannick Engelhardt", "Rocco Reitz", "Joe Scally", "Kevin Stoger",
    "Florian Neuhaus", "Haris Tabakovic", "Hugo Bolin", "Gio Reyna",
    "Tim Kleindienst",
    "Budu Zivzivadze", "Marnon Busch", "Patrick Mainka", "Niklas Dorsch",
    "Eren Dinkci", "Jonas Fohrenbach", "Julian Niehues", "Marvin Pieringer",
    "Diant Ramaj", "Mathias Honsak", "Hennes Behrens", "Leonidas Stergiou",
    "Arijon Ibrahimovic", "Mikkel Kaufmann", "Benedikt Gimber",
    "Frank Schmidt", "Oigan Polanski", "Bastian Dankert",
    "Nordkurve", "Ruven Schroder",
    # Surnames (single tokens)
    "Honorat", "Mohya", "Castrop", "Machino", "Elvedi", "Nicolas",
    "Diks", "Sander", "Engelhardt", "Reitz", "Scally", "Stoger",
    "Neuhaus", "Tabakovic", "Bolin", "Reyna",
    "Zivzivadze", "Busch", "Mainka", "Dorsch", "Dinkci", "Fohrenbach",
    "Niehues", "Pieringer", "Ramaj", "Honsak", "Behrens", "Stergiou",
    "Ibrahimovic", "Kaufmann", "Gimber", "Dankert",
    # Extra terms Deepgram mangles
    "St. Pauli", "Sankt Pauli", "Freiburg",
    "Rheinland", "Koln", "Cologne",
    "Bosnia", "Herzegovina", "Georgian",
    "relegation", "last-gasp", "matchdays",
]

# ─── Deterministic corrections ───────────────────────────────────────────

CORRECTIONS = [
    # ─── Team name misrecognitions ───
    ("Honsakovic in the blue", "Heidenheim in the blue"),
    ("Honsenheim in the blue", "Heidenheim in the blue"),
    ("Zivadze in the blue", "Heidenheim in the blue"),
    ("Flag back all in white", "Gladbach all in white"),
    ("Fanback all in white", "Gladbach all in white"),
    ("Flagback all in white", "Gladbach all in white"),
    ("Flankert all in white", "Gladbach all in white"),
    ("Flag back", "Gladbach"),
    ("Fanback", "Gladbach"),
    ("Flagback", "Gladbach"),
    ("Flankert", "Gladbach"),
    ("At Back of", "Gladbach have"),
    ("Tabakov picked up", "Gladbach have picked up"),
    ("Saks Paoli", "St. Pauli"),
    ("Saks Pauly", "St. Pauli"),
    ("Fallen Elf", "Fohlenelf"),
    # ─── Bundesliga / league terms ───
    ("Gundesliga", "Bundesliga"),
    ("Rock Blossom", "Rock Bottom"),
    ("relegated battle", "relegation battle"),
    ("in the lead.", "in the league."),
    ("in the lead,", "in the league,"),
    ("in that side,", "in that time,"),
    # ─── Score / match references ───
    ("last guest winner", "last-gasp winner"),
    ("laxed gasp winner", "last-gasp winner"),
    ("at laxed gasp", "a last-gasp"),
    ("Not one a game", "Not won a game"),
    ("three hole draw", "three-all draw"),
    ("three o draw", "three-all draw"),
    ("four seed Bundesliga", "fourteen Bundesliga"),
    ("15.27 games", "15 points from 27 games"),
    ("beat 5.21", "beat Freiburg 2-1"),
    # ─── Rival / location names ───
    ("Brightman rivals, Curl", "Rheinland Rivals, Koln"),
    ("Brightland rivals, Curl", "Rheinland Rivals, Koln"),
    ("Brightman rivals, Koln", "Rheinland Rivals, Koln"),
    ("Brightland rivals, Koln", "Rheinland Rivals, Koln"),
    ("at Brightman.", "at Rheinland Rivals, Koln."),
    ("at Brightman ", "at Rheinland Rivals, Koln "),
    # ─── Bosnia / Herzegovina ───
    ("Bolznier Herzegovina", "Bosnia-Herzegovina"),
    ("Bolznik, Honsakovic", "Bosnia-Herzegovina"),
    ("Bolznik Honsakovic", "Bosnia-Herzegovina"),
    ("heroic self pulse. Near Herzegovina", "heroics helping Bosnia-Herzegovina"),
    ("heroic self in Bosnia", "heroics helping Bosnia"),
    # ─── Player / person names ───
    ("Ubijzivzivadze", "Budu Zivzivadze"),
    ("Budu, Zivzivadze", "Budu Zivzivadze"),
    ("Mubu Zivzivadze", "Budu Zivzivadze"),
    ("Mubi Zivzivadze", "Budu Zivzivadze"),
    ("Chortion appendage", "Georgian appendage"),
    ("Georgia appendage", "Georgian appendage"),
    ("Bolt Bastian national GT in South Korea", "Bolin has been on international duty with South Korea"),
    ("Korea is fit for this one", "Bolin is fit for this one"),
    # ─── Commentary phrasing fixes ───
    ("big six in for", "this season for"),
    ("Big six in for", "This season for"),
    ("the by Engelhardt", "the captain. Forward by Engelhardt"),
    ("Falled by", "Fouled by"),
    ("Fanged way back", "Banged away back"),
    ("Bright Shuto", "Shuto"),
    ("in a run.", "in a row."),
    ("He's on a Way through", "He's on his way through"),
]


def apply_corrections(text):
    for wrong, right in CORRECTIONS:
        text = text.replace(wrong, right)
    return text


# ─── Translation ─────────────────────────────────────────────────────────

TRANSLATE_SYSTEM = """You are a real-time translator for live soccer commentary.
Translate the English soccer commentary to {lang_name}. Rules:
1. Keep player names, team names, and proper nouns unchanged (Gladbach, Heidenheim, Diks, Reitz, Honorat, etc.)
2. Maintain the energy and rhythm of live commentary — this will be spoken aloud by TTS
3. Use natural soccer terminology for the target language
4. Return ONLY the translation, no explanations
5. Keep it concise — match the length of the original"""

LANG_NAMES = {
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


def translate_utterance(client, text, lang="es"):
    """Translate a single utterance. Returns (translated_text, latency_seconds)."""
    lang_name = LANG_NAMES.get(lang, lang)
    t_start = time.time()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM.format(lang_name=lang_name)},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        max_tokens=512,
    )
    t_end = time.time()
    return response.choices[0].message.content.strip(), t_end - t_start


# ─── Audio helpers ────────────────────────────────────────────────────────

def convert_to_pcm(audio_path):
    pcm_path = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path,
         "-ar", "16000", "-ac", "1", "-f", "wav", pcm_path],
        capture_output=True,
    )
    return pcm_path


def pcm_chunks_realtime(wav_path, chunk_ms=100):
    bytes_per_sec = 32000
    chunk_bytes = int(bytes_per_sec * chunk_ms / 1000)
    chunk_duration = chunk_ms / 1000.0

    with open(wav_path, "rb") as f:
        f.read(44)  # skip WAV header
        audio_offset = 0.0
        t_start = time.monotonic()
        while True:
            data = f.read(chunk_bytes)
            if not data:
                break
            yield data, audio_offset
            audio_offset += chunk_duration
            target = t_start + audio_offset
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)


# ─── Main pipeline ────────────────────────────────────────────────────────

def run_pipeline(audio_path, deepgram_key, lang="es"):
    os.environ["DEEPGRAM_API_KEY"] = deepgram_key
    from deepgram import DeepgramClient
    from deepgram.listen import ListenV1Results, ListenV1UtteranceEnd
    import deepgram as dg_module

    # Convert audio
    print("Converting to PCM...")
    pcm_path = convert_to_pcm(audio_path)
    pcm_size = os.path.getsize(pcm_path)
    duration_s = (pcm_size - 44) / 32000
    lang_name = LANG_NAMES.get(lang, lang)

    print(f"Audio: {audio_path} (~{duration_s:.0f}s)")
    print(f"Pipeline: Deepgram STT → Corrections → GPT-4o-mini → {lang_name}")
    print(f"Streaming at real-time speed — will take ~{duration_s:.0f} seconds\n")

    # Clients
    dg_client = DeepgramClient()
    oai_client = openai.OpenAI()

    # Results storage
    utterances = []
    wall_start = None

    # Translation thread pool — translate utterances as they arrive
    translate_lock = threading.Lock()

    def translate_worker(utt_entry):
        """Translate a single utterance and record timing."""
        corrected = utt_entry["corrected"]
        t_translate_start = time.time()
        translated, translate_time = translate_utterance(oai_client, corrected, lang)
        t_translate_end = time.time()

        utt_entry["translated"] = translated
        utt_entry["translate_time"] = round(translate_time, 3)
        utt_entry["tts_ready_wall"] = round(t_translate_end - wall_start, 3)
        utt_entry["total_latency"] = round(
            utt_entry["tts_ready_wall"] - utt_entry["audio_end"], 3
        )

        with translate_lock:
            audio_t = utt_entry["audio_start"]
            stt_lat = utt_entry["stt_latency"]
            tl_lat = utt_entry["translate_time"]
            total = utt_entry["total_latency"]
            en_preview = corrected[:45]
            es_preview = translated[:45]
            print(f"  [{audio_t:6.1f}s] stt={stt_lat:.2f}s "
                  f"xlat={tl_lat:.2f}s total={total:.2f}s")
            print(f"           EN: {en_preview}")
            print(f"           {lang.upper()}: {es_preview}")

    # Connect to Deepgram
    print("[DEEPGRAM] Connecting...")
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

        # Start audio feed
        wall_start = time.time()
        audio_thread = threading.Thread(target=feed_audio, daemon=True)
        audio_thread.start()
        print("[DEEPGRAM] Streaming started.\n")

        translate_threads = []

        for msg in ws:
            if not isinstance(msg, ListenV1Results):
                continue

            # Only process final results (skip interim for translation)
            if not msg.is_final:
                continue

            alt = msg.channel.alternatives[0]
            transcript = alt.transcript
            if not transcript:
                continue

            wall_now = time.time() - wall_start
            audio_start = msg.start if hasattr(msg, "start") and msg.start else 0
            audio_duration = msg.duration if hasattr(msg, "duration") and msg.duration else 0
            audio_end = audio_start + audio_duration
            stt_latency = wall_now - audio_end

            # Apply deterministic corrections
            corrected = apply_corrections(transcript)

            entry = {
                "audio_start": round(audio_start, 3),
                "audio_end": round(audio_end, 3),
                "stt_wall": round(wall_now, 3),
                "stt_latency": round(stt_latency, 3),
                "raw_en": transcript,
                "corrected": corrected,
                "is_final": msg.is_final,
                "speech_final": msg.speech_final,
                "confidence": alt.confidence,
                "words": [
                    {
                        "word": w.punctuated_word if hasattr(w, "punctuated_word") else w.word,
                        "confidence": w.confidence,
                        "start": w.start,
                        "end": w.end,
                    }
                    for w in alt.words
                ],
            }
            utterances.append(entry)

            # Launch translation in parallel thread
            t = threading.Thread(target=translate_worker, args=(entry,), daemon=True)
            translate_threads.append(t)
            t.start()

        # Wait for all translations to complete
        for t in translate_threads:
            t.join()

    # Cleanup
    os.unlink(pcm_path)

    return utterances


# ─── Report ───────────────────────────────────────────────────────────────

def print_report(utterances, audio_path, lang):
    lang_name = LANG_NAMES.get(lang, lang)

    # Filter to only translated utterances
    translated = [u for u in utterances if "total_latency" in u]

    if not translated:
        print("\nNo utterances were translated.")
        return

    stt_lats = [u["stt_latency"] for u in translated]
    xlat_lats = [u["translate_time"] for u in translated]
    total_lats = [u["total_latency"] for u in translated]

    print(f"\n{'=' * 90}")
    print(f"  REAL-TIME TRANSLATION PIPELINE: {os.path.basename(audio_path)}")
    print(f"  Pipeline: Deepgram → Corrections → GPT-4o-mini → {lang_name}")
    print(f"  Utterances: {len(translated)}")
    print(f"{'=' * 90}")

    # Latency breakdown
    print(f"\n{'─── LATENCY BREAKDOWN (seconds) ───':─<90}")
    print(f"  {'Stage':<25} {'Mean':>8} {'Median':>8} {'P90':>8} {'P95':>8} {'Max':>8} {'Min':>8}")
    print(f"  {'-' * 73}")

    for name, lats in [("Deepgram STT", stt_lats),
                        ("GPT-4o-mini translate", xlat_lats),
                        ("TOTAL (audio→TTS-ready)", total_lats)]:
        s = sorted(lats)
        print(f"  {name:<25} "
              f"{statistics.mean(s):8.2f} "
              f"{statistics.median(s):8.2f} "
              f"{s[int(len(s) * 0.9)]:8.2f} "
              f"{s[int(len(s) * 0.95)]:8.2f} "
              f"{max(s):8.2f} "
              f"{min(s):8.2f}")

    # Corrections applied
    corrections_made = sum(1 for u in translated if u["raw_en"] != u["corrected"])
    print(f"\n  Corrections applied: {corrections_made}/{len(translated)} utterances modified")

    # Per-utterance timeline
    print(f"\n{'─── UTTERANCE TIMELINE ───':─<90}")
    print(f"  {'Audio':>7} {'STT':>6} {'Xlat':>6} {'Total':>6}  EN → {lang.upper()}")
    print(f"  {'-' * 84}")

    for u in translated:
        audio_t = u["audio_start"]
        stt = u["stt_latency"]
        xlat = u["translate_time"]
        total = u["total_latency"]
        en = u["corrected"][:35]
        tr = u.get("translated", "")[:35]
        print(f"  {audio_t:6.1f}s {stt:5.2f}s {xlat:5.2f}s {total:5.2f}s  {en}")
        print(f"  {'':>28}  {tr}")

    # 3-second delay analysis
    print(f"\n{'─── 3-SECOND DELAY BUDGET ───':─<90}")
    within_3s = len([t for t in total_lats if t <= 3.0])
    within_2s = len([t for t in total_lats if t <= 2.0])
    within_1_5s = len([t for t in total_lats if t <= 1.5])
    print(f"  Utterances within 1.5s: {within_1_5s}/{len(total_lats)} "
          f"({within_1_5s / len(total_lats) * 100:.0f}%)")
    print(f"  Utterances within 2.0s: {within_2s}/{len(total_lats)} "
          f"({within_2s / len(total_lats) * 100:.0f}%)")
    print(f"  Utterances within 3.0s: {within_3s}/{len(total_lats)} "
          f"({within_3s / len(total_lats) * 100:.0f}%)")

    # Save results
    print(f"\n{'─── SAVED FILES ───':─<90}")

    json_path = f"stt_rt_translate_{lang}.json"
    with open(json_path, "w") as f:
        json.dump(translated, f, indent=2, default=str)
    print(f"  {json_path}")

    en_path = f"stt_rt_translate_{lang}_english.txt"
    with open(en_path, "w") as f:
        f.write(" ".join(u["corrected"] for u in translated))
    print(f"  {en_path}")

    tr_path = f"stt_rt_translate_{lang}_translated.txt"
    with open(tr_path, "w") as f:
        f.write(" ".join(u.get("translated", "") for u in translated))
    print(f"  {tr_path}")

    print(f"\n{'=' * 90}")


# ─── Main ─────────────────────────────────────────────────────────────────

def speak_to_agent(backend_url, agent_id, text):
    """Push text to avatar TTS via /speak INTERRUPT."""
    url = f"{backend_url}/speak"
    payload = json.dumps({
        "agent_id": agent_id,
        "text": text,
        "priority": "INTERRUPT",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception:
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Real-time STT → Correct → Translate pipeline"
    )
    parser.add_argument("--audio", required=True, help="Path to audio file")
    parser.add_argument("--lang", default="es", help="Target language (es, fr, de, pt, etc.)")
    parser.add_argument("--agent-id", help="If set, send translations to /speak endpoint")
    parser.add_argument("--backend", default="http://localhost:8082", help="Backend URL")
    parser.add_argument(
        "--deepgram-key",
        default=os.environ.get("DEEPGRAM_API_KEY", ""),
    )
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        print(f"File not found: {args.audio}")
        sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set")
        sys.exit(1)

    utterances = run_pipeline(args.audio, args.deepgram_key, args.lang)

    # If agent-id provided, also send each translated utterance to /speak
    if args.agent_id:
        print(f"\nSending {len(utterances)} utterances to agent {args.agent_id}...")
        for u in utterances:
            text = u.get("translated", u.get("corrected", ""))
            if text:
                speak_to_agent(args.backend, args.agent_id, text)

    print_report(utterances, args.audio, args.lang)


if __name__ == "__main__":
    main()

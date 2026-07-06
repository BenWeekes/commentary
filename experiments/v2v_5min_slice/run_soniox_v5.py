#!/usr/bin/env python3
"""Run Soniox v5 real-time on the 5-min slice, with translation to French.

Captures:
- English transcript (original tokens)
- French transcript (translation tokens)
- Per-token timing (start_ms, end_ms)
- Speaker labels
- Wall-clock arrival per token for latency analysis
"""
from __future__ import annotations
import json, os, sys, threading, time
from urllib.parse import quote
from websockets.sync.client import connect

API_KEY = open("/home/ubuntu/soniox").read().strip()
URL = "wss://stt-rt.soniox.com/transcribe-websocket"

PCM_PATH = "/tmp/v2v_compare/slice_5min.pcm"
OUT_TOKENS = "/tmp/v2v_compare/soniox_v5_tokens.jsonl"
OUT_EN = "/tmp/v2v_compare/soniox_v5_en.txt"
OUT_FR = "/tmp/v2v_compare/soniox_v5_fr.txt"

# Same roster as Gemini probe (so comparison is fair)
ROSTER = [
    "Mwene", "Philipp Mwene", "Posch", "Stefan Posch", "Sano", "Kaishu Sano",
    "Nebel", "Paul Nebel", "Amiri", "Nadiem Amiri", "Caci", "Anthony Caci",
    "Tietz", "Philipp Tietz", "da Costa", "Danny da Costa", "Becker",
    "Sheraldo Becker", "Zentner", "Robin Zentner", "Kohr", "Dominik Kohr",
    "Jae-sung Lee", "Sieb", "Armindo Sieb", "Maloney", "Lennard Maloney",
    "Veratschnig", "Nikolas Veratschnig", "Kawasaki", "Sota Kawasaki",
    "Widmer", "Silvan Widmer", "Weiper", "Nelson Weiper", "Potulski",
    "Kacper Potulski", "Leite", "Diogo Leite", "Doekhi", "Danilho Doekhi",
    "Kemlein", "Aljoscha Kemlein", "Burke", "Oliver Burke", "Khedira",
    "Rani Khedira", "Skarke", "Tim Skarke", "Kral", "Alex Kral", "Nsoki",
    "Stanley Nsoki", "Kohn", "Derrick Kohn", "Wisbereit", "Tom Wisbereit",
    "Juranovic", "Josip Juranovic", "Trimmel", "Christopher Trimmel", "Jeong",
    "Woo-yeong Jeong", "Schafer", "Andras Schafer", "Querfeld", "Leopold Querfeld",
    "Klaus", "Carl Klaus", "Ilic", "Andrej Ilic",
    "Mainz", "FSV Mainz", "Union", "Union Berlin",
    "Mewa Arena", "Marie-Louise Eta", "Urs Fischer", "Florian Exner",
]


def make_config():
    return {
        "api_key": API_KEY,
        "model": "stt-rt-v5",
        "language_hints": ["en"],
        "enable_language_identification": True,
        "enable_speaker_diarization": True,
        "enable_endpoint_detection": True,
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
        "num_channels": 1,
        "context": {
            "general": [
                {"key": "domain", "value": "Bundesliga football match commentary"},
                {"key": "match", "value": "FSV Mainz vs Union Berlin"},
            ],
            "terms": ROSTER,
            "text": "Live English football commentary covering FSV Mainz vs Union Berlin at the Mewa Arena.",
        },
        "translation": {
            "type": "one_way",
            "target_language": "fr",
        },
    }


def main():
    pcm = open(PCM_PATH, "rb").read()
    print(f"Input: {len(pcm)/16000/2:.1f}s of 16kHz mono")
    config = make_config()
    print(f"Model: {config['model']}, target: {config['translation']['target_language']}")

    final_tokens = []
    all_token_records = []  # (wall_t, token_dict)

    with connect(URL, max_size=64 * 1024 * 1024, ping_interval=None) as ws:
        ws.send(json.dumps(config))
        t0 = time.time()

        def sender():
            CHUNK = 3200  # 100ms
            for i in range(0, len(pcm), CHUNK):
                target = t0 + (i / 32000.0)
                wait = target - time.time()
                if wait > 0: time.sleep(wait)
                try:
                    ws.send(pcm[i:i+CHUNK])
                except Exception as e:
                    print(f"send err: {e}")
                    return
            try:
                ws.send("")  # empty string signals end-of-audio
            except Exception: pass

        threading.Thread(target=sender, daemon=True).start()

        try:
            while True:
                msg = ws.recv()
                d = json.loads(msg)
                if d.get("error_code"):
                    print(f"ERROR: {d['error_code']}: {d.get('error_message')}")
                    break
                wall = time.time() - t0
                for tok in d.get("tokens", []):
                    rec = {
                        "wall_t": round(wall, 3),
                        "text": tok.get("text"),
                        "start_ms": tok.get("start_ms"),
                        "end_ms": tok.get("end_ms"),
                        "is_final": tok.get("is_final"),
                        "speaker": tok.get("speaker"),
                        "language": tok.get("language"),
                        "translation_status": tok.get("translation_status"),
                        "confidence": tok.get("confidence"),
                    }
                    if tok.get("is_final"):
                        final_tokens.append(rec)
                    all_token_records.append(rec)
                if d.get("finished"):
                    print(f"Session finished at t={wall:.1f}s")
                    break
        except Exception as e:
            print(f"recv loop: {e}")

    # Save raw token stream
    with open(OUT_TOKENS, "w") as f:
        for rec in all_token_records:
            f.write(json.dumps(rec) + "\n")

    # Separate EN (original) and FR (translation) FINAL tokens
    en_finals = [t for t in final_tokens
                 if t.get("translation_status") in (None, "original", "none")
                 and (t.get("language") in (None, "en"))]
    fr_finals = [t for t in final_tokens if t.get("translation_status") == "translation"]

    # Also: terminal tokens like <end>/<fin> shouldn't be counted
    def is_terminal(t):
        return t.get("text") in ("<end>", "<fin>", "<endpoint>")

    en_finals = [t for t in en_finals if not is_terminal(t)]
    fr_finals = [t for t in fr_finals if not is_terminal(t)]

    en_text = "".join(t["text"] or "" for t in en_finals).strip()
    fr_text = "".join(t["text"] or "" for t in fr_finals).strip()
    open(OUT_EN, "w").write(en_text)
    open(OUT_FR, "w").write(fr_text)

    # Latency stats: per-token wall_t arrival vs audio time start_ms
    if en_finals:
        lags = []
        for t in en_finals:
            if t.get("start_ms") is not None:
                lags.append(t["wall_t"] - t["start_ms"]/1000.0)
        if lags:
            lags.sort()
            p50 = lags[len(lags)//2]
            p90 = lags[int(len(lags)*0.9)]
            print(f"Per-token finalization lag (wall - audio_start):")
            print(f"  p50={p50:.2f}s p90={p90:.2f}s max={max(lags):.2f}s")

    print(f"\nFinal EN tokens: {len(en_finals)}, words: ~{len(en_text.split())}")
    print(f"Final FR tokens: {len(fr_finals)}, words: ~{len(fr_text.split())}")
    print(f"\nEN preview: {en_text[:300]!r}")
    print(f"\nFR preview: {fr_text[:300]!r}")
    print(f"\nSaved: {OUT_TOKENS}, {OUT_EN}, {OUT_FR}")


if __name__ == "__main__":
    main()

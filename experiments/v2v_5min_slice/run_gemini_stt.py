#!/usr/bin/env python3
"""Test gemini-3.1-flash-lite-live-translate (S2ST WizLive) as a low-latency
STT (+FR translation) source vs Soniox.

The earlier failed test used the CONVERSATIONAL model (gemini-3.1-flash-live-preview)
which dropped ~75% of input via turn-management. This is the purpose-built live
TRANSLATION model (strict real-time latency), the follow-up the docs flagged.
inputAudioTranscription => English STT (the thing we want); outputAudioTranscription
=> French. Measures coverage + tail latency vs gold.

Usage:
  GEMINI_API_KEY=... python run_gemini_stt.py [seconds]   # seconds of audio to feed (default all 300)
"""
from __future__ import annotations
import base64, json, os, sys, threading, time
from urllib.parse import quote
from websockets.sync.client import connect

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "models/gemini-3.1-flash-lite-live-translate"
LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
PCM_PATH = "/tmp/v2v_compare/slice_5min.pcm"
OUT_EVENTS = "/tmp/v2v_compare/gemini_stt_events.jsonl"


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    pcm = open(PCM_PATH, "rb").read()[: int(seconds * 32000)]
    audio_dur = len(pcm) / 32000.0
    print(f"Model: {MODEL}\nFeeding {audio_dur:.1f}s of 16kHz mono at real-time")

    # inputAudioTranscription / outputAudioTranscription / translationConfig are
    # SETUP-level fields (siblings of generationConfig), not inside it.
    setup = {"setup": {
        "model": MODEL,
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "translationConfig": {"targetLanguageCode": "fr", "echoTargetLanguage": True}},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }}
    url = f"{LIVE_URL}?key={quote(API_KEY)}"
    events = []; in_txt = []; out_txt = []; out_audio = 0

    with connect(url, max_size=64 * 1024 * 1024, ping_interval=None) as ws:
        ws.send(json.dumps(setup))
        while True:
            msg = json.loads(ws.recv(timeout=15))
            if "setupComplete" in msg or "setup_complete" in msg:
                break
            if "error" in msg:
                print(f"SETUP ERROR: {msg['error']}"); return
        t0 = time.time(); done = threading.Event()

        def sender():
            CHUNK = 3200  # 100ms
            for i in range(0, len(pcm), CHUNK):
                wait = (t0 + i / 32000.0) - time.time()
                if wait > 0: time.sleep(wait)
                try:
                    ws.send(json.dumps({"realtimeInput": {"audio": {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(pcm[i:i+CHUNK]).decode()}}}))
                except Exception as e:
                    print(f"send err: {e}"); return
            try: ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            except Exception: pass
            done.set()
        threading.Thread(target=sender, daemon=True).start()

        deadline = t0 + audio_dur + 20.0
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=3.0))
            except TimeoutError:
                if done.is_set() and time.time() > t0 + audio_dur + 6: break
                continue
            except Exception as e:
                print(f"recv err: {e}"); break
            wall = round(time.time() - t0, 2)
            sc = msg.get("serverContent") or {}
            it = sc.get("inputTranscription", {}).get("text")
            if it:
                in_txt.append(it); events.append({"t": wall, "kind": "in", "text": it})
            ot = sc.get("outputTranscription", {}).get("text")
            if ot:
                out_txt.append(ot); events.append({"t": wall, "kind": "out", "text": ot})
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                d = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
                if d: out_audio += len(base64.b64decode(d))
            if sc.get("interrupted"):
                events.append({"t": wall, "kind": "interrupted"})

    with open(OUT_EVENTS, "w") as f:
        for e in events: f.write(json.dumps(e, ensure_ascii=False) + "\n")

    en = "".join(in_txt); fr = "".join(out_txt)
    n_in = len(en.split()); n_out = len(fr.split())
    interr = sum(1 for e in events if e["kind"] == "interrupted")
    last_in = max((e["t"] for e in events if e["kind"] == "in"), default=0)
    print(f"\n=== RESULT ({audio_dur:.0f}s audio) ===")
    print(f"EN transcript (STT): {n_in} words   FR transcript: {n_out} words   out-audio: {out_audio/48000:.1f}s")
    print(f"interruptions: {interr}   last input-transcript at wall {last_in:.1f}s (audio ended {audio_dur:.1f}s) -> tail lag {last_in-audio_dur:+.1f}s")
    print(f"\nEN STT sample: {en[:300]!r}")
    print(f"FR sample:     {fr[:200]!r}")


if __name__ == "__main__":
    main()

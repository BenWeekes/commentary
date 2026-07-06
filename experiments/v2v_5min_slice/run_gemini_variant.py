#!/usr/bin/env python3
"""Run Gemini Live on the 5-min slice with one of several config variants.

Variants (pick via $VARIANT):
  base       — same as previous run (default VAD, AUDIO modality, default activity handling)
  no_interrupt — activityHandling: NO_INTERRUPTION
  manual_vad — disable automaticActivityDetection; send single activityStart/End for the whole stream
  text_only  — responseModalities: TEXT only (no Gemini audio output)
"""
from __future__ import annotations
import base64, json, os, sys, threading, time, wave
from urllib.parse import quote
from websockets.sync.client import connect

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "models/gemini-3.1-flash-live-preview"
LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

VARIANT = os.environ.get("VARIANT", "base")
PCM_PATH = "/tmp/v2v_compare/slice_5min.pcm"
OUT_DIR = "/tmp/v2v_compare"
OUT_WAV = f"{OUT_DIR}/gemini_{VARIANT}_fr_audio.wav"
OUT_EVENTS = f"{OUT_DIR}/gemini_{VARIANT}_events.jsonl"

# Same tightened prompt as the base run
SYSTEM_PROMPT = """Translate the incoming live English football commentary into French.

Players on the field include:
Mwene, Philipp Mwene, Posch, Stefan Posch, Sano, Kaishu Sano, Nebel, Paul Nebel,
Amiri, Nadiem Amiri, Caci, Anthony Caci, Tietz, Philipp Tietz, da Costa,
Danny da Costa, Becker, Sheraldo Becker, Zentner, Robin Zentner, Kohr,
Dominik Kohr, Jae-sung Lee, Sieb, Armindo Sieb, Maloney, Lennard Maloney,
Veratschnig, Nikolas Veratschnig, Kawasaki, Sota Kawasaki, Widmer, Silvan Widmer,
Weiper, Nelson Weiper, Potulski, Kacper Potulski, Leite, Diogo Leite,
Doekhi, Danilho Doekhi, Kemlein, Aljoscha Kemlein, Burke, Oliver Burke,
Khedira, Rani Khedira, Skarke, Tim Skarke, Kral, Alex Kral, Nsoki, Stanley Nsoki,
Kohn, Derrick Kohn, Wisbereit, Tom Wisbereit, Juranovic, Josip Juranovic,
Trimmel, Christopher Trimmel, Jeong, Woo-yeong Jeong, Schafer, Andras Schafer,
Querfeld, Leopold Querfeld, Klaus, Carl Klaus, Ilic, Andrej Ilic.

Teams: Mainz, FSV Mainz, Union, Union Berlin. Venue: Mewa Arena.
Coaches: Marie-Louise Eta, Urs Fischer. Referee: Florian Exner.

Rules:
1. Translate the meaning faithfully into natural spoken French.
2. Render English football idioms as natural French equivalents, not literally.
3. Preserve all player and team names exactly as listed above.
4. Do NOT invent details, actions, players, events, or commentary that is not in the source.
5. Output only the translated French. No greetings, no apologies, no explanations.
6. If translation cannot be produced, output exactly __TRANSLATION_FAILED__."""


def build_setup():
    generation_config = {
        "responseModalities": ["TEXT"] if VARIANT == "text_only" else ["AUDIO"],
    }
    if VARIANT != "text_only":
        generation_config["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}
        }

    setup_body = {
        "model": MODEL,
        "generationConfig": generation_config,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "inputAudioTranscription": {},
    }
    # Output transcription only makes sense when there is audio output
    if VARIANT != "text_only":
        setup_body["outputAudioTranscription"] = {}

    # realtimeInputConfig variants
    if VARIANT == "no_interrupt":
        setup_body["realtimeInputConfig"] = {
            "activityHandling": "NO_INTERRUPTION",
        }
    elif VARIANT == "manual_vad":
        setup_body["realtimeInputConfig"] = {
            "automaticActivityDetection": {"disabled": True},
        }
    return {"setup": setup_body}


def main():
    pcm = open(PCM_PATH, "rb").read()
    print(f"[{VARIANT}] Input: {len(pcm)/16000/2:.1f}s of 16kHz mono")

    setup = build_setup()
    print(f"[{VARIANT}] Setup: {json.dumps(setup, indent=2)[:300]}...")

    url = f"{LIVE_URL}?key={quote(API_KEY)}"
    output_pcm = bytearray()
    output_rate = 24000
    events = []

    with connect(url, max_size=64 * 1024 * 1024, ping_interval=None) as ws:
        ws.send(json.dumps(setup))
        while True:
            msg = json.loads(ws.recv(timeout=15))
            if "setupComplete" in msg or "setup_complete" in msg:
                break
            if "error" in msg:
                print(f"[{VARIANT}] ERROR: {msg['error']}")
                return

        t0 = time.time()
        events.append({"t": 0.0, "kind": "setup_complete", "payload": ""})
        done = threading.Event()

        def sender():
            CHUNK = 3200
            # If manual VAD, send single activityStart at beginning
            if VARIANT == "manual_vad":
                try:
                    ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))
                    events.append({"t": time.time() - t0, "kind": "activity_start", "payload": ""})
                except Exception as e:
                    print(f"[{VARIANT}] activityStart err: {e}")
            for i in range(0, len(pcm), CHUNK):
                target = t0 + (i / 32000.0)
                wait = target - time.time()
                if wait > 0: time.sleep(wait)
                if i == 0:
                    events.append({"t": time.time() - t0, "kind": "first_pcm", "payload": ""})
                try:
                    ws.send(json.dumps({
                        "realtimeInput": {
                            "audio": {"mimeType": "audio/pcm;rate=16000",
                                      "data": base64.b64encode(pcm[i:i+CHUNK]).decode()}
                        }
                    }))
                except Exception as e:
                    print(f"[{VARIANT}] send err: {e}")
                    return
            try:
                if VARIANT == "manual_vad":
                    ws.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))
                    events.append({"t": time.time() - t0, "kind": "activity_end", "payload": ""})
                ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            except Exception: pass
            events.append({"t": time.time() - t0, "kind": "audio_stream_end", "payload": ""})
            done.set()

        threading.Thread(target=sender, daemon=True).start()

        audio_dur = len(pcm) / 32000.0
        deadline = t0 + audio_dur + 60.0  # extra grace for variants that may have tail

        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=3.0))
            except TimeoutError:
                if done.is_set() and time.time() > t0 + audio_dur + 15:
                    break
                continue
            except Exception as e:
                print(f"[{VARIANT}] recv err: {e}")
                break
            wall = time.time() - t0
            sc = msg.get("serverContent") or {}
            in_t = sc.get("inputTranscription", {}).get("text")
            if in_t:
                events.append({"t": round(wall, 3), "kind": "in", "payload": in_t})
            out_t = sc.get("outputTranscription", {}).get("text")
            if out_t:
                events.append({"t": round(wall, 3), "kind": "out", "payload": out_t})
            # For text_only, the translation lives in modelTurn parts as text
            mt = sc.get("modelTurn") or {}
            for part in mt.get("parts") or []:
                # text content (for text_only mode)
                if "text" in part:
                    events.append({"t": round(wall, 3), "kind": "text_part", "payload": part["text"]})
                inline = part.get("inlineData") or part.get("inline_data") or {}
                d = inline.get("data")
                if d:
                    raw = base64.b64decode(d)
                    mime = inline.get("mimeType") or ""
                    if "rate=" in mime:
                        try: output_rate = int(mime.split("rate=")[1].split(";")[0])
                        except: pass
                    if not output_pcm:
                        events.append({"t": round(wall, 3), "kind": "first_out_audio",
                                       "payload": f"rate={output_rate}"})
                    output_pcm.extend(raw)
            if sc.get("interrupted"):
                events.append({"t": round(wall, 3), "kind": "interrupted", "payload": ""})
            if sc.get("turnComplete"):
                events.append({"t": round(wall, 3), "kind": "turn_complete", "payload": ""})
            if sc.get("generationComplete"):
                events.append({"t": round(wall, 3), "kind": "gen_complete", "payload": ""})

    # Save audio (if any)
    if output_pcm:
        with wave.open(OUT_WAV, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(output_rate)
            w.writeframes(bytes(output_pcm))
        audio_s = len(output_pcm) / 2 / output_rate
        print(f"[{VARIANT}] Saved {audio_s:.1f}s of audio to {OUT_WAV}")
    with open(OUT_EVENTS, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    in_text = "".join(e["payload"] for e in events if e["kind"]=="in")
    out_text = "".join(e["payload"] for e in events if e["kind"]=="out")
    text_parts = "".join(e["payload"] for e in events if e["kind"]=="text_part")

    from collections import Counter
    counts = Counter(e["kind"] for e in events)
    print(f"[{VARIANT}] Event kinds: {dict(counts)}")
    print(f"[{VARIANT}] EN transcript: {len(in_text)} chars, ~{len(in_text.split())} words")
    if VARIANT == "text_only":
        print(f"[{VARIANT}] FR text parts: {len(text_parts)} chars, ~{len(text_parts.split())} words")
    else:
        print(f"[{VARIANT}] FR transcript: {len(out_text)} chars, ~{len(out_text.split())} words")
        print(f"[{VARIANT}] FR audio: {len(output_pcm)/2/output_rate:.1f}s")


if __name__ == "__main__":
    main()

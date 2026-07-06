#!/usr/bin/env python3
"""Run Gemini Live on the 5-min m05_uni slice and capture everything.

Tightened prompt: roster + idiom guidance but no "speak with energy" line,
to test if hallucinations drop vs the earlier probe.
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

PCM_PATH = "/tmp/v2v_compare/slice_5min.pcm"
OUT_WAV = "/tmp/v2v_compare/gemini_fr_audio.wav"
OUT_EVENTS = "/tmp/v2v_compare/gemini_events.jsonl"

# Tightened prompt: roster present, but no stylistic invitation to invent content
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


def main():
    pcm = open(PCM_PATH, "rb").read()
    print(f"Input: {len(pcm)/16000/2:.1f}s of 16kHz mono")

    setup = {
        "setup": {
            "model": MODEL,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}
                },
            },
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
    }

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
                print(f"ERROR: {msg['error']}")
                return

        t0 = time.time()
        events.append({"t": 0.0, "kind": "setup_complete", "payload": ""})
        done = threading.Event()

        def sender():
            CHUNK = 3200  # 100ms
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
                    print(f"send err: {e}")
                    return
            try:
                ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            except Exception: pass
            events.append({"t": time.time() - t0, "kind": "audio_stream_end", "payload": ""})
            done.set()

        threading.Thread(target=sender, daemon=True).start()

        audio_dur = len(pcm) / 32000.0
        deadline = t0 + audio_dur + 30.0  # extra grace for tail of output

        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=3.0))
            except TimeoutError:
                if done.is_set() and time.time() > t0 + audio_dur + 10:
                    break
                continue
            except Exception as e:
                print(f"recv err: {e}")
                break
            wall = time.time() - t0
            sc = msg.get("serverContent") or {}
            in_t = sc.get("inputTranscription", {}).get("text")
            if in_t:
                events.append({"t": round(wall, 3), "kind": "in", "payload": in_t})
            out_t = sc.get("outputTranscription", {}).get("text")
            if out_t:
                events.append({"t": round(wall, 3), "kind": "out", "payload": out_t})
            if sc.get("interrupted"):
                events.append({"t": round(wall, 3), "kind": "interrupted", "payload": ""})
            mt = sc.get("modelTurn") or {}
            for part in mt.get("parts") or []:
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
            if sc.get("turnComplete"):
                events.append({"t": round(wall, 3), "kind": "turn_complete", "payload": ""})
            if sc.get("generationComplete"):
                events.append({"t": round(wall, 3), "kind": "gen_complete", "payload": ""})

    # Save audio
    with wave.open(OUT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(output_rate)
        w.writeframes(bytes(output_pcm))
    # Save events
    with open(OUT_EVENTS, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    in_text = "".join(e["payload"] for e in events if e["kind"]=="in")
    out_text = "".join(e["payload"] for e in events if e["kind"]=="out")
    audio_s = len(output_pcm) / 2 / output_rate
    print(f"\nSaved: {OUT_WAV} ({audio_s:.1f}s of audio at {output_rate}Hz)")
    print(f"Saved: {OUT_EVENTS} ({len(events)} events)")
    print(f"\n[EN transcript] {len(in_text)} chars: {in_text[:200]}...")
    print(f"\n[FR transcript] {len(out_text)} chars: {out_text[:200]}...")


if __name__ == "__main__":
    main()

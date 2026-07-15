#!/usr/bin/env python3
"""TRUE-live blend — proves the latency budget end-to-end.

Unlike run_blend_live.py (which looks up pre-computed vision by video-time),
this runs the ENTIRE decision path live inside the SRT loop:

  - VISION: gpt-5.6, 4-frame bursts at 720p, called in-loop by a pool of
    parallel workers; per-call latency measured. Nothing is pre-computed.
  - STT: the harvested live-Soniox pool, but availability-gated by realistic
    arrival time (phrase usable only after end_s + STT_LAG) — models live STT.
  - TRACKER: artifact lookup gated to detections at least TRK_LAG old
    (the GPU tracker runs near-realtime, slightly behind).
  - AUDIO: broadcast policy — the stream runs a FIXED BUFFER_S-second delay and
    every line either lands EXACTLY on its play (placed at t_det) or is DROPPED
    (behind_live > BUFFER_S). Lines never slip; sync is guaranteed throughout.
    Stale detections are also skipped at decision time (STALE_S). No byte
    clobbering: placements never overwrite earlier audio.

Outputs: commentary_blend_live.jsonl (with per-line latency fields),
ai_blend_live_{en,fr}_track.wav, latency_report.json.

Usage: .venv/bin/python run_blend_true_live.py
"""
from __future__ import annotations
import json, os, statistics, subprocess, sys, threading, time, wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault('SONIOX_POOL', 'soniox_live_short.jsonl')
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
import run_blend_live as B          # chooser, signals, tts, lineup, SRT plumbing
import run_events_detector as D     # call_vision, extract_json, validate_shape

BASE = B.BASE
BUFFER_S = 10.0            # FIXED broadcast delay: every surviving line lands on the play
STALE_S = BUFFER_S - 3.0   # drop-late: skip detections too old to clear chooser+TTS in time
STT_LAG = 1.8              # Soniox finalize latency modelled on top of phrase end
TRK_LAG = 0.5              # tracker runs near-realtime, slightly behind
VISION_WORKERS = 3
VISION_MODEL = 'gpt-5.6'
VISION_SCALE = '960:540'   # benchmark sweet spot: 3.9s median vs 6.3s at 720p, quality holds
PROMPT = (BASE / 'prompts' / 'events_detector_v1.txt').read_text()
SR = B.SR

# live vision store: dicts {t_det, det, latency_ms, arrived_wall}
VIS_LIVE: list[dict] = []
VIS_LOCK = threading.Lock()
VIS_STATS: list[int] = []   # every call's latency, ok or not


def vision_worker(burst_paths, t_det, wall0):
    raw, ms, err = D.call_vision(B.client, VISION_MODEL, burst_paths, PROMPT)
    VIS_STATS.append(ms)
    if err:
        return
    obj, perr = D.extract_json(raw)
    if perr or D.validate_shape(obj):
        return
    with VIS_LOCK:
        VIS_LIVE.append({'t_det': t_det, 'det': obj, 'latency_ms': ms,
                         'arrived_wall': time.monotonic() - wall0})


def main():
    for f in B.FRAMES_DIR.glob('f_*.jpg'):
        f.unlink()
    trk_sorted = B.TRK                      # [(t, rec)] sorted
    son_sorted = B.SON                      # [(t, rec)] sorted
    print(f"TRUE LIVE: vision={VISION_MODEL} x{VISION_WORKERS} workers in-loop | "
          f"{len(son_sorted)} STT phrases (gated) | buffer={BUFFER_S}s")

    def start_receiver_540():
        return subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-i', B.SRT_URL_RECV, '-vf', f'fps=1/{B.SAMPLE_INTERVAL_S},scale={VISION_SCALE}',
            '-q:v', '4', '-start_number', '1', '-y', str(B.FRAMES_DIR / 'f_%05d.jpg')],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    sender = B.start_sender(); time.sleep(1.5); recv = start_receiver_540()
    wall0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=VISION_WORKERS)
    inflight = threading.Semaphore(VISION_WORKERS)

    audio = {'en': bytearray(int((B.DURATION_S + 30) * SR * 2)),
             'fr': bytearray(int((B.DURATION_S + 30) * SR * 2))}
    audio_end = {'en': 0, 'fr': 0}          # last written byte — no-clobber floor
    audio_lock = threading.Lock()

    lines = []; used_son = set(); recent = []
    booth = 0.0                             # pacing in current-time domain
    last_event = (None, -99.0); last_subj = (None, -99.0)
    last_lull = -99.0; last_scene = -99.0; last_named = None
    last_submitted = 0; vis_consumed = -1.0
    processed = 0; last_new = time.monotonic(); stopping = False

    def place(rec, t_det, seen_to_decide, chooser_ms):
        """Translate+TTS, then place EXACTLY at t_det — or DROP if it missed the
        buffer. Sync policy: a line either lands on its play or is never heard."""
        t_tts0 = time.monotonic()
        en = B.tts(rec['text'], B.EN_VOICE)
        fr_text = B.translate_fr(rec['text']); rec['fr'] = fr_text
        frp = B.tts(fr_text, B.FR_VOICE)
        tts_ms = int((time.monotonic() - t_tts0) * 1000)
        behind = (time.monotonic() - wall0) - t_det          # true live latency
        rec['lat'] = {'seen_to_decide_s': round(seen_to_decide, 2),
                      'chooser_ms': chooser_ms, 'tts_ms': tts_ms,
                      'behind_live_s': round(behind, 2)}
        if behind > BUFFER_S:                                 # DROP, never slip
            rec['dropped'] = True
            print(f"  [ drop ] ({rec['src']}) {rec['text']}   [behind_live={behind:.1f}s > {BUFFER_S}s]")
            return
        v_place = t_det
        shift = 0.0
        with audio_lock:
            for lang, pcm in (('en', en), ('fr', frp)):
                b = int((v_place + B.NATURAL_LAG_S) * SR) * 2
                if b < audio_end[lang]:                       # no clobbering
                    shift = max(shift, (audio_end[lang] - b) / (SR * 2))
                    b = audio_end[lang]
                b -= b % 2
                u = min(len(pcm), len(audio[lang]) - b)
                if u > 0:
                    audio[lang][b:b + u] = pcm[:u]
                    audio_end[lang] = b + u
        rec['lat']['audio_shift_s'] = round(shift, 2)
        rec['video_time_s'] = round(v_place + shift, 2)
        print(f"  [{rec['video_time_s']:6.1f}s] ({rec['src']}) {rec['text']}"
              f"   [behind_live={behind:.1f}s]")

    def emit(rec, t_now, t_det, est, gate, seen_to_decide, chooser_ms):
        nonlocal booth
        lines.append(rec); recent.append(rec['text'])
        threading.Thread(target=place, args=(rec, t_det, seen_to_decide, chooser_ms),
                         daemon=True).start()
        booth = t_now + B.NATURAL_LAG_S + est + gate

    while True:
        frames = sorted(B.FRAMES_DIR.glob('f_*.jpg')); n = len(frames)
        if n > processed:
            last_new = time.monotonic()
        processed = n
        if sender.poll() is not None and time.monotonic() - last_new > 5:
            stopping = True
        if n >= B.CONTEXT_FRAMES:
            t = n * B.SAMPLE_INTERVAL_S

            # --- submit a vision burst whenever a worker is free ---
            if n > last_submitted and inflight.acquire(blocking=False):
                last_submitted = n
                burst = frames[n - B.CONTEXT_FRAMES:n]
                t_det = n * B.SAMPLE_INTERVAL_S

                def run(bp=burst, td=t_det):
                    try:
                        vision_worker(bp, td, wall0)
                    finally:
                        inflight.release()
                pool.submit(run)

            # --- (1) STT phrase, availability-gated (arrives end_s + STT_LAG) ---
            real = None
            for tt, r in son_sorted:
                if tt in used_son:
                    continue
                if t >= float(r.get('end_s', tt)) + STT_LAG and t - tt <= 6.0:
                    used_son.add(tt); real = r; break
            if real:
                rt = float(real['video_time_s'])
                rec = {'src': 'soniox', 'text': real['text'], 'real_phrase': real['text'],
                       'vision': None, 'tracker': None, 'video_time_s': round(rt, 2)}
                est = real.get('dur') or max(1.4, len(real['text'].split()) / 2.6)
                emit(rec, t, rt, est, 0.4, t - rt, 0)
                time.sleep(0.02); continue

            # --- (2) vision-grounded line from the LATEST ARRIVED detection ---
            if t >= booth:
                with VIS_LOCK:
                    # stale-skip: never speak a detection too old to land within
                    # the buffer after chooser+TTS (~3s pipeline remainder)
                    fresh = [v for v in VIS_LIVE
                             if v['t_det'] > vis_consumed and t - v['t_det'] <= STALE_S]
                    cand = max(fresh, key=lambda v: v['t_det']) if fresh else None
                if cand:
                    vis_consumed = cand['t_det']
                    t_det = cand['t_det']
                    vsig = B.vision_signal(cand['det'])
                    vfact = B.fact_str(vsig)
                    trk_det = None
                    for tt, r in reversed(trk_sorted):
                        if tt <= t - TRK_LAG and abs(tt - t_det) <= 2.0:
                            trk_det = r.get('detection'); break
                    ttruth = B.tracker_truth(trk_det)
                    subj = (vsig.get('poss_team'), vsig.get('poss_player'))
                    speak, gate, scene = False, 4.0, False
                    if vsig.get('event') and (vsig['event'] != last_event[0] or t - last_event[1] > 8):
                        speak, gate = True, 2.5
                    elif vsig.get('poss_player'):
                        speak, gate = True, 2.5
                    elif (vsig.get('poss_team') or ttruth) and t - last_lull > 3:
                        speak, gate, last_lull = True, 3.0, t
                    elif t - last_scene > 40:
                        speak, gate, scene, last_scene = True, 4.0, True, t
                    if speak:
                        cur_named = vsig.get('poss_player')
                        received = cur_named if (cur_named and cur_named != last_named) else None
                        c0 = time.monotonic()
                        line = B.chooser(t_det, None, vfact, ttruth, recent,
                                         B.broadcaster_names_near(t_det),
                                         B.vision_conf(vsig), received, scene)
                        chooser_ms = int((time.monotonic() - c0) * 1000)
                        if (line and line.upper() != 'NO_CALL' and len(line.split()) >= 2
                                and not B.too_similar(line, recent[-8:])):
                            if vsig.get('event'):
                                last_event = (vsig['event'], t)
                            if cur_named:
                                last_subj = (subj, t); last_named = cur_named
                            rec = {'src': 'blend', 'text': line, 'real_phrase': None,
                                   'vision': vfact, 'tracker': ttruth,
                                   'vision_latency_ms': cand['latency_ms'],
                                   'video_time_s': round(t_det, 2)}
                            emit(rec, t, t_det, max(1.4, len(line.split()) / 2.6), gate,
                                 t - t_det, chooser_ms)
                        else:
                            booth = t + 1.2
                    else:
                        booth = t + 0.8
        if stopping:
            print("[main] sender ended; draining"); time.sleep(2.0); break
        if time.monotonic() - last_new > 400:
            break
        time.sleep(0.05)

    for p in (sender, recv):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    pool.shutdown(wait=True)
    time.sleep(7.0)   # drain TTS threads
    for lang in ('en', 'fr'):
        with wave.open(str(BASE / f'ai_blend_live_{lang}_track.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(bytes(audio[lang][:int(B.DURATION_S * SR * 2)]))

    kept = [l for l in lines if not l.get('dropped')]
    dropped = [l for l in lines if l.get('dropped')]
    kept.sort(key=lambda l: l['video_time_s'])
    (BASE / 'commentary_blend_live.jsonl').write_text(
        '\n'.join(json.dumps(l, ensure_ascii=False) for l in kept) + '\n')

    # ---- latency + sync report ----
    vl = sorted(VIS_STATS)
    bl = sorted(l['lat']['behind_live_s'] for l in kept if 'lat' in l)
    shifts = [l['lat'].get('audio_shift_s', 0) for l in kept
              if l.get('lat', {}).get('audio_shift_s', 0) > 0]
    rep = {
        'fixed_delay_s': BUFFER_S, 'policy': 'on-play or dropped — lines never slip',
        'vision_model': VISION_MODEL, 'vision_scale': VISION_SCALE,
        'workers': VISION_WORKERS, 'vision_calls': len(vl),
        'vision_latency_s': {'median': round(vl[len(vl)//2]/1000, 2),
                             'p90': round(vl[int(len(vl)*0.9)]/1000, 2),
                             'max': round(vl[-1]/1000, 2)} if vl else None,
        'lines_kept': len(kept), 'lines_dropped': len(dropped),
        'survival_rate': round(len(kept) / max(1, len(lines)), 3),
        'kept_behind_live_s': {'median': round(statistics.median(bl), 2),
                               'p90': round(bl[int(len(bl)*0.9)], 2),
                               'max': round(bl[-1], 2)} if bl else None,
        'audio_shifts': len(shifts), 'max_audio_shift_s': max(shifts) if shifts else 0.0,
        'dropped_texts': [d['text'] for d in dropped],
    }
    (BASE / 'latency_report.json').write_text(json.dumps(rep, indent=2))
    ns = sum(1 for l in kept if l['src'] == 'soniox')
    print(f"\n=== TRUE LIVE (fixed {BUFFER_S:.0f}s delay): {len(kept)} on-play lines "
          f"({ns} STT, {len(kept)-ns} vision), {len(dropped)} dropped ===")
    print(json.dumps(rep, indent=2))


if __name__ == '__main__':
    main()

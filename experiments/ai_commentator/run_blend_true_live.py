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
BUFFER_S = float(os.environ.get('BLEND_DELAY_S', '10.0'))   # FIXED broadcast delay (deployment knob): every surviving line lands on the play
FAST_PROFILE = BUFFER_S <= 7.0
# 6s fast profile: mini-structured vision (2.4s vs 5.5s) + guards for its known
# weaknesses (team claims need tracker agreement; naming high-conf only)
STALE_S = BUFFER_S - (2.4 if FAST_PROFILE else 3.0)
STT_LAG = 1.5 if FAST_PROFILE else 1.8   # tighter finalize window at 6s; long phrases may drop
USE_STT = os.environ.get('USE_STT', '1') != '0'   # Alex's vision/tracker-only variant sets USE_STT=0
TRK_LAG = 0.5              # tracker runs near-realtime, slightly behind
VISION_WORKERS = 4   # 540p payloads are light; 4 workers halves burst-skip for brief events
VISION_MODEL = 'gpt-5.4-mini' if FAST_PROFILE else 'gpt-5.6'
VISION_SCALE = '960:540'   # benchmark sweet spot: 3.9s median vs 6.3s at 720p, quality holds

# ---- MODE: conservative (default) vs eager — same grounding rules, different pacing/style ----
MODE = os.environ.get('BLEND_MODE', 'eager')   # R9 (accepted): eager is the default voice
K = {'conservative': dict(lull=3.0, scene=40.0, event_regate=8.0, poss_gate=3.0, retreat=1.2),
     'eager':        dict(lull=2.0, scene=22.0, event_regate=6.0, poss_gate=2.2, retreat=0.8)}[MODE]
SUFFIX = ('' if MODE == 'conservative' else '_eager') + ('_6s' if FAST_PROFILE else '') + ('' if USE_STT else '_vt') + os.environ.get('RUN_TAG', '')
if MODE == 'eager':
    B.CHOOSER_SYSTEM += """

EAGER STYLE MODE: aim for a flowing broadcast feel. Prefer 8-16 word lines with two
connected clauses ("Kohn collects it and looks for the switch out left"). Vary your
openings — never start consecutive lines the same way, and don't always lead with the
team name. Weave in brief colour (crowd, tension, the clock) sparingly. All facts still
come ONLY from the menu — the style is eager, the grounding is not."""

# grounded opener from the verified scoreboard (76:50, M05 1-1 FCU) — no vision needed
OPENER = ("Mainz and Union, level at one apiece — quarter of an hour to go."
          if FAST_PROFILE else
          "Back underway at the Mewa Arena — Mainz and Union level at one apiece, "
          "just over a quarter of an hour to play.")

# ---- EAGER final stage: a COMMENTATOR, not a chooser ----------------------------
# Completely separate final-LLM architecture from the safe mode: a stronger model,
# a rolling WINDOW of recent grounded observations (motion, not a snapshot), and
# its own timed commentary history so lines can build a narrative.
EAGER_MODEL = 'gpt-5.5'            # falls back to gpt-5.4-mini when the detection is old
EAGER_SYSTEM = f"""You are the sole live English TV commentator for this Bundesliga match:
Mainz (red, home) vs Union Berlin (olive, away) at the Mewa Arena. Referee Florian Exner.
MATCH CLOCK: {B.MATCH_CLOCK}.

Each moment you receive: YOUR RECENT COMMENTARY (timestamped — what the viewer already
heard), a WINDOW of grounded observations from the last few seconds (vision facts with
times, tracker position/shape), possibly the player who just received the ball, and any
names the human broadcaster used nearby.

Write the ONE line a top broadcaster would say NOW (4-16 words), or exactly NO_CALL.

CRAFT — this is what separates you from a caption generator:
- Build on your own recent lines: continue a thread, note a change, complete an arc
  ("still Mainz... — and NOW Union win it back"). Never restate what you just said.
- Describe the window as MOTION and intent, not a frozen snapshot: where play has moved,
  who is pressing, what the shape suggests is coming.
- Vary sentence architecture line to line: sometimes a name first, sometimes the action,
  sometimes a short punch ("Won back instantly."). Never open consecutive lines alike.
- Do not reuse any verb in AVOID VERBS. Clock/scoreline at most once every two minutes.

GROUNDING — hard rules, never bend them:
- Speak ONLY facts present in the WINDOW, tracker line, or roster below. No invented
  events, shots, saves, goals, or scoreline changes. If the window is empty or unclear,
  NO_CALL.
- Name a player ONLY if the window or broadcaster-names provide them. Never guess.

ROSTER (number, name, team, position):
{B.ROSTER_BLOCK}
Output only the line, or NO_CALL."""

# Reviewer-derived generation rules (tuning_rules.yaml R2, R4, R5, R6) — appended to
# BOTH final stages so the safe fallback obeys them too.
GEN_RULES = """

REVIEWER RULES (hard requirements):
- R2 CONTENT FLOOR: every line must contain at least one concrete piece of information
  (a named player, an event, a location or shape change). Never emit empty filler like
  "Midfield battle continues here" or "Quiet spell, still all square" — prefer NO_CALL.
- R4 CONTINUITY: if possession or momentum has FLIPPED relative to your previous line,
  mark the transition explicitly ("Union win it back", "turned over") — never assert
  the opposite state as if your previous line did not exist.
- R5 PRONOUNS: no pronoun without an explicit antecedent in this or the previous line.
  When in doubt, use the concrete noun (the ball, the cross, the free kick).
- R6 MANNER RESTRAINT: never state HOW an action was performed (long/short, driven,
  floated, calmly, powerfully) unless the facts explicitly provide it. Use
  manner-neutral verbs ("plays it forward", not "launches it long").
- R12 TEAM ATTRIBUTION: every player in the ROSTER belongs to exactly one team. When you
  name a player together with a team-specific event — a booking/card, goal, substitution,
  free kick, corner, throw — the team you state MUST be that player's team as listed in the
  ROSTER. Look the name up before you attribute it; a named player NEVER belongs to the
  opponent. If unsure of the team, name the player or the event without a team, not a guess.
- R13 NO CAMERA / PICTURE: never describe the picture. Do NOT say "in the frame", "in shot",
  "in the picture", or merely list which players are visible without an action they are
  performing ("X, Y and Z in the frame"). That is the camera, not the play — prefer NO_CALL."""
TEAM_FORMS = {   # R11 — grounded in pre-match data (kits: Mainz red, Union olive/green)
    'Mainz': ['Mainz', 'FSV Mainz', 'the hosts', 'the home side', 'the reds'],
    'Union': ['Union', 'Union Berlin', 'the visitors', 'the away side', 'the men in green'],
}
TEAM_VARIETY = """

R11 TEAM-REFERENCE VARIETY: when referring to a team, rotate between its APPROVED forms
— Mainz: Mainz / FSV Mainz / the hosts / the home side / the reds;
  Union: Union / Union Berlin / the visitors / the away side / the men in green.
Never use the identical team reference in consecutive lines about the same team. Use ONLY
these forms — no invented nicknames. Kit-colour forms ("the reds") are welcome variety."""
GEN_RULES += TEAM_VARIETY
EAGER_SYSTEM += GEN_RULES
B.CHOOSER_SYSTEM += GEN_RULES

# R12 HARD GUARD (deterministic, roster-grounded): the prompt rule alone is unreliable on
# the 6s mini model, so we also ENFORCE attribution in code. If a card/goal/substitution
# line credits an event 'for/pour <team>' that contradicts the named player's roster team,
# strip that clause. Generic — resolves off the pre-match lineup, no match facts hardcoded.
import re as _reatt
SUR2TEAM = {v['name']: v['team'] for v in B.LINEUP.values() if len(v.get('name', '')) >= 3}
_CGS_RX = _reatt.compile(r'yellow|red card|\bbooked\b|\bbook\b|sent off|dismissed|'
                         r'\bgoal\b(?!\s*kick)|scored|substitut|\bsub(bed)?\b', _reatt.I)

def enforce_attribution(text):
    if not text or not _CGS_RX.search(text):
        return text
    named = [s for s in SUR2TEAM if _reatt.search(r'\b' + _reatt.escape(s) + r'\b', text)]
    if not named:
        return text
    for team, forms in TEAM_FORMS.items():
        for fm in forms:
            if (_reatt.search(r'\b(for|pour)\s+' + _reatt.escape(fm) + r'\b', text, _reatt.I)
                    and any(SUR2TEAM[s] != team for s in named)):
                text = _reatt.sub(r'\s*\b(for|pour)\s+' + _reatt.escape(fm) + r'\b', '',
                                  text, flags=_reatt.I)
    return _reatt.sub(r'\s+([.,;:!?])', r'\1', text).strip()

# R7: French localizer is the single canonical B.TRANSLATE_SYSTEM (run_blend_live.py) —
# no override here, so the glossary lives in exactly one place.


def eager_commentator(t_det, window, ttruth, recent_timed, bnames, received, avoid, age_s):
    """The eager final stage. window = [(t_rel, fact_str), ...] oldest->newest."""
    wl = "\n".join(f"  - [{tr:+.1f}s] {f}" for tr, f in window) or "  - (nothing certain)"
    rc = "\n".join(f"  [{tt:6.1f}s] {tx}" for tt, tx in recent_timed[-8:]) or "  (nothing yet)"
    user = (f"MOMENT t={t_det:.0f}s\n"
            f"WINDOW of grounded observations (relative to now):\n{wl}\n"
            f"- tracker(truth): {ttruth or '(no read)'}\n"
            f"- pass just received by: {received or '(nobody new)'}\n"
            f"- broadcaster just named: {', '.join(bnames) if bnames else '(nobody)'}\n"
            f"- AVOID VERBS: {', '.join(avoid) if avoid else '(none yet)'}\n"
            f"YOUR RECENT COMMENTARY:\n{rc}\nLine:")
    model = 'gpt-5.4-mini' if (FAST_PROFILE or age_s > 5.0) else EAGER_MODEL
    try:
        r = B.client.responses.create(model=model, instructions=EAGER_SYSTEM,
                                      input=[{"role": "user", "content": user}],
                                      max_output_tokens=300,
                                      reasoning={"effort": "low"})
        import re as _re
        return _re.sub(r'\s+', ' ', (r.output_text or '').strip().strip('"'))
    except Exception as e:
        print(f"  eager-commentator err: {e}")
        return 'NO_CALL'


PRIORITY_EVENTS = {'yellow_card', 'red_card', 'goal', 'penalty'}   # R1
import re as _re4
POSS_RX = _re4.compile(r'\b(Mainz|Union)\b.{0,30}\b(possess|keep|on the ball|have it|work|circulat|hold)', _re4.I)
TRANS_RX = _re4.compile(r'win|won|regain|turn|steal|intercept|back|break|rob|force', _re4.I)
_SANE_CACHE = {}

def stt_sane(text, event_type):
    """R8: when a verbatim STT phrase coincides with a high-confidence event, ask a
    fast model whether the phrase is sensible football English (ASR errors like
    'changes of foot' during a substitution must not propagate into two languages)."""
    if text in _SANE_CACHE:
        return _SANE_CACHE[text]
    try:
        r = B.client.responses.create(model='gpt-5.4-mini',
            instructions=("Answer YES or NO only. YES unless this is CLEARLY a garbled "
                          "speech-recognition error (nonsense words, impossible grammar). "
                          "Idioms, colloquialisms and commentator flourishes are all YES. "
                          "Example NO: 'Meanwhile, changes of foot.' (garble of 'changes "
                          "afoot' during a substitution). Example YES: 'Going to be caught "
                          "every day of the week by the keeper.' "
                          "Context: a '" + str(event_type) + "' event is on."),
            input=[{"role": "user", "content": text}], max_output_tokens=150,
            reasoning={"effort": "low"})
        ok = 'YES' in (r.output_text or 'YES').upper()
    except Exception:
        ok = True
    _SANE_CACHE[text] = ok
    return ok


def prewarm():
    """Open TLS/connections for TTS, translate and vision BEFORE the stream starts,
    so the first real line doesn't pay cold-start latency (it cost us the first
    STT anchor: 11.2s behind on a 10s window)."""
    sample = sorted((BASE / 'frames').glob('f_*.jpg'))[:4]
    ths = [threading.Thread(target=lambda: B.tts('Ready.', B.EN_VOICE)),
           threading.Thread(target=lambda: B.tts('Prêt.', B.FR_VOICE)),
           threading.Thread(target=lambda: B.translate_fr('Ready to go.')),
           threading.Thread(target=lambda: B.tts('Pronto.', B.PT_VOICE)),
           threading.Thread(target=lambda: B.translate_pt('Ready to go.'))]
    # R8 with zero in-loop latency: pre-vet every pool phrase now (parallel, cached)
    for _, r in B.SON:
        ths.append(threading.Thread(target=stt_sane, args=(r['text'], 'the current play')))
    if sample:
        ths.append(threading.Thread(target=lambda: D.call_vision(B.client, VISION_MODEL, sample, PROMPT)))
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=25)
PROMPT = (BASE / 'prompts' / 'events_detector_v1.txt').read_text()
SR = B.SR

# live vision store: dicts {t_det, det, latency_ms, arrived_wall}
VIS_LIVE: list[dict] = []
VIS_LOCK = threading.Lock()
VIS_STATS: list[int] = []   # every call's latency, ok or not


def vision_worker(burst_paths, t_det, wall0):
    if FAST_PROFILE:
        D.MAX_OUTPUT_TOKENS = 300
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
    print(f"TRUE LIVE [{MODE}]: vision={VISION_MODEL} x{VISION_WORKERS} workers in-loop | "
          f"{len(son_sorted)} STT phrases (gated) | buffer={BUFFER_S}s")
    print("prewarming TTS/translate/vision connections...")
    prewarm()

    def start_receiver_540():
        return subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-i', B.SRT_URL_RECV, '-vf', f'fps=1/{B.SAMPLE_INTERVAL_S},scale={VISION_SCALE}',
            '-q:v', '4', '-start_number', '1', '-y', str(B.FRAMES_DIR / 'f_%05d.jpg')],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    sender = B.start_sender(); time.sleep(1.5); recv = start_receiver_540()
    wall0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=VISION_WORKERS)
    chooser_pool = ThreadPoolExecutor(max_workers=4)   # eager hedge: 2 concurrent final stages
    tts_pool = ThreadPoolExecutor(max_workers=12)      # per-line EN/FR/PT speech, best-effort
    inflight = threading.Semaphore(VISION_WORKERS)

    audio = {L: bytearray(int((B.DURATION_S + 30) * SR * 2)) for L in ('en', 'fr', 'pt')}
    audio_end = {'en': 0, 'fr': 0, 'pt': 0}          # last written byte — no-clobber floor
    audio_lock = threading.Lock()
    write_cond = threading.Condition()
    in_flight = {}                          # token -> v_place; writes commit in v_place order

    lines = []; used_son = set(); recent = []
    booth = 0.0                             # pacing in current-time domain
    last_team_spoken = None   # F8/R4: explicit possession-flip signal for the stages
    last_form_used = {}       # R11: team -> exact reference form used in the last line about them
    last_prio = {}     # R1: event-type -> last narration time (30s, team-agnostic)
    last_events = {}   # R3: (event_type, team) -> {'t':, 'named':} — 25s dedup w/ new-info escape
    last_subj = (None, -99.0)
    last_lull = -99.0; last_scene = -99.0; last_named = None
    last_submitted = 0; vis_consumed = -1.0
    processed = 0; last_new = time.monotonic(); stopping = False
    opener_done = False                     # scripted, scoreboard-grounded opening line
    placed_end = 0.0                        # video-time where the last placed line's audio ends

    def place(rec, t_det, seen_to_decide, chooser_ms, est=3.0):
        try:
            _place_inner(rec, t_det, seen_to_decide, chooser_ms, est)
        finally:
            with write_cond:
                in_flight.pop(id(rec), None); write_cond.notify_all()

    def _place_inner(rec, t_det, seen_to_decide, chooser_ms, est=3.0):
        """Translate+TTS, then place EXACTLY at t_det — or DROP if it missed the
        buffer. Sync policy: a line either lands on its play or is never heard."""
        rec['text'] = enforce_attribution(rec['text'])   # R12 hard guard before EN/FR/PT
        t_tts0 = time.monotonic()
        def _en():
            return B.tts(rec['text'], B.EN_VOICE)
        def _fr():
            txt = B.translate_fr(rec['text']); return txt, B.tts(txt, B.FR_VOICE)
        def _pt():
            txt = B.translate_pt(rec['text']); return txt, B.tts(txt, B.PT_VOICE)
        f_en = tts_pool.submit(_en); f_fr = tts_pool.submit(_fr); f_pt = tts_pool.submit(_pt)
        # EN is the PRIMARY track and gates placement. FR/PT are best-effort within the
        # remaining live budget - if they can't keep up they go SILENT for this line
        # (logged), rather than delaying/dropping an otherwise-good line. This decouples
        # survival from the slower two languages (fixes the 3-track latency tax) and never
        # counts a line 'kept' with a mandatory track missing.
        now_behind = (time.monotonic() - wall0) - t_det
        en_deadline = max(0.3, BUFFER_S - now_behind - 0.2)
        try:
            en = f_en.result(timeout=en_deadline)
        except Exception:
            en = b''
        behind = (time.monotonic() - wall0) - t_det          # measured at EN readiness
        missing = []
        grace = max(0.05, BUFFER_S - behind - 0.1)
        def _side(fut, lang):
            try:
                txt, pcm = fut.result(timeout=grace); return txt, pcm
            except Exception:
                missing.append(lang); return '', b''
        rec['fr'], frp = _side(f_fr, 'fr')
        rec['pt'], ptp = _side(f_pt, 'pt')
        tts_ms = int((time.monotonic() - t_tts0) * 1000)
        rec['lat'] = {'seen_to_decide_s': round(seen_to_decide, 2),
                      'chooser_ms': chooser_ms, 'tts_ms': tts_ms,
                      'behind_live_s': round(behind, 2),
                      'missing_tracks': missing}
        if not en:                                            # no primary audio -> drop
            rec['dropped'] = True
            print(f"  [ drop ] ({rec['src']}) {rec['text']}   [EN TTS missed budget]")
            with write_cond:
                in_flight.pop(id(rec), None); write_cond.notify_all()
            return
        # F11: commit writes in PLACEMENT order — a later-timed line must not raise
        # the floor before an earlier-timed line has written (completion-order race)
        tok = id(rec)
        deadline = time.monotonic() + 5.0
        with write_cond:
            while any(vp < t_det - 1e-6 for tk, vp in in_flight.items() if tk != tok):
                if time.monotonic() > deadline:
                    # an earlier-placed line is stuck; dropping THIS later line preserves
                    # write order (never write out of sequence)
                    rec['dropped'] = True
                    print(f"  [ drop ] write-order stall; dropping later line {rec['text'][:30]!r}")
                    in_flight.pop(id(rec), None); write_cond.notify_all()
                    return
                write_cond.wait(timeout=0.3)
        cap = int((est * 2.5 + 3.0) * SR) * 2   # F7: a TTS blob can never exceed its speech slot
        if len(en) > cap:
            print(f"  [ trunc ] EN audio {len(en)/(SR*2):.1f}s capped to {cap/(SR*2):.1f}s: {rec['text'][:40]!r}")
            en = en[:cap]
        if len(frp) > cap:
            print(f"  [ trunc ] FR audio {len(frp)/(SR*2):.1f}s capped to {cap/(SR*2):.1f}s: {rec['text'][:40]!r}")
            frp = frp[:cap]
        if len(ptp) > cap:
            print(f"  [ trunc ] PT audio {len(ptp)/(SR*2):.1f}s capped to {cap/(SR*2):.1f}s: {rec['text'][:40]!r}")
            ptp = ptp[:cap]
        rec['lat']['audio_s'] = round(len(en) / (SR * 2), 2)
        rec['lat']['audio_fr_s'] = round(len(frp) / (SR * 2), 2)
        if behind > BUFFER_S:                                 # DROP, never slip
            rec['dropped'] = True
            print(f"  [ drop ] ({rec['src']}) {rec['text']}   [behind_live={behind:.1f}s > {BUFFER_S}s]")
            with write_cond:
                in_flight.pop(id(rec), None); write_cond.notify_all()
            return
        v_place = t_det
        with audio_lock:
            # decide the shift for BOTH tracks first; a shift is only acceptable if it
            # stays within a natural speech gap — beyond that the line is DESYNCED, so
            # drop it (also stops one bad write from cascading down the whole track)
            bases = {}
            shift = 0.0
            for lang in ('en', 'fr', 'pt'):
                b = int((v_place + B.NATURAL_LAG_S) * SR) * 2
                b -= b % 2
                bases[lang] = b
                if b < audio_end[lang]:
                    shift = max(shift, (audio_end[lang] - b) / (SR * 2))
            if shift > 1.5:                                   # would desync — drop instead
                rec['dropped'] = True
                print(f"  [ drop ] ({rec['src']}) {rec['text']}   [shift {shift:.1f}s would desync]")
                with write_cond:
                    in_flight.pop(id(rec), None); write_cond.notify_all()
                return
            for lang, pcm in (('en', en), ('fr', frp), ('pt', ptp)):
                b = max(bases[lang], audio_end[lang]); b -= b % 2
                u = min(len(pcm), len(audio[lang]) - b)
                if u > 0:
                    audio[lang][b:b + u] = pcm[:u]
                    audio_end[lang] = b + u
        rec['lat']['audio_shift_s'] = round(shift, 2)
        rec['video_time_s'] = round(v_place + shift, 2)
        with write_cond:
            in_flight.pop(id(rec), None); write_cond.notify_all()
        print(f"  [{rec['video_time_s']:6.1f}s] ({rec['src']}) {rec['text']}"
              f"   [behind_live={behind:.1f}s]")

    def emit(rec, t_now, t_det, est, gate, seen_to_decide, chooser_ms):
        nonlocal booth, placed_end
        placed_end = max(placed_end, t_det + B.NATURAL_LAG_S + est + 0.15)
        lines.append(rec); recent.append(rec['text'])
        with write_cond:
            in_flight[id(rec)] = t_det
        threading.Thread(target=place, args=(rec, t_det, seen_to_decide, chooser_ms, est),
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

            # --- (0) scripted opener: verified scoreboard facts, no vision needed ---
            if not opener_done:
                opener_done = True
                rec = {'src': 'blend', 'text': OPENER, 'real_phrase': None,
                       'vision': 'match context (scoreboard: 77th min, 1-1)',
                       'tracker': None, 'video_time_s': 0.8}
                emit(rec, t, 0.8, max(1.4, len(OPENER.split()) / 2.6), 1.5, t - 0.8, 0)
                time.sleep(0.02); continue

            # --- (1) STT phrase, availability-gated (arrives end_s + STT_LAG) ---
            # USE_STT=0 -> vision/tracker-only variant: skip verbatim STT entirely.
            real = None
            if USE_STT:
                for tt, r in son_sorted:
                    if tt in used_son:
                        continue
                    if t >= float(r.get('end_s', tt)) + STT_LAG and t - tt <= 6.0:
                        used_son.add(tt)
                        if placed_end - tt > 1.4:
                            continue          # slot already occupied — would desync, skip
                        real = r; break
            if real:
                rt = float(real['video_time_s'])
                # R8: vet the phrase if a high-confidence event overlaps it
                ev_near = None
                with VIS_LOCK:
                    for v in VIS_LIVE:
                        if abs(v['t_det'] - rt) <= 3.5:
                            vs = B.vision_signal(v['det'])
                            if vs.get('event') and vs.get('event_conf') == 'high':
                                ev_near = vs['event']; break
                if not stt_sane(real['text'], ev_near or 'the current play'):
                    print(f"  [ veto ] (soniox) {real['text']!r} — ASR-suspect"
                          + (f" during {ev_near}" if ev_near else ''))
                    continue
                rec = {'src': 'soniox', 'text': real['text'], 'real_phrase': real['text'],
                       'vision': None, 'tracker': None, 'video_time_s': round(rt, 2)}
                est = real.get('dur') or max(1.4, len(real['text'].split()) / 2.6)
                emit(rec, t, rt, est, 0.4, t - rt, 0)
                time.sleep(0.02); continue

            # --- (1.5) R1: HIGH-conf card/goal/penalty preempts pacing — never skipped ---
            prio = None
            with VIS_LOCK:
                goal_sightings = [v2['t_det'] for v2 in VIS_LIVE
                                  if any(e.get('type') == 'goal' and e.get('confidence') == 'high'
                                         for e in (v2['det'].get('events') or []))
                                  and t - v2['t_det'] <= 15.0]
                for v in VIS_LIVE:
                    if t - v['t_det'] > STALE_S:
                        continue
                    # scan the RAW events list — a card listed behind a foul must
                    # still be found (vision_signal only surfaces the first event)
                    for e in (v['det'].get('events') or []):
                        et, etm = e.get('type'), B.TEAM.get(e.get('team'))
                        if (et in PRIORITY_EVENTS and e.get('confidence') == 'high'
                                and t - last_prio.get(et, -99.0) > 30.0):
                            if et == 'goal':
                                # R10: adjacent bursts share frames — a goal call needs
                                # >=3 high-conf sightings SPANNING >=5s (net + celebration
                                # + aftermath). A 2-burst blip is not a goal.
                                if len(goal_sightings) < 3 or (max(goal_sightings) - min(goal_sightings)) < 5.0:
                                    continue
                            prio = (v, et, etm); break
                    if prio:
                        break
            if prio:
                v, et, etm = prio
                last_prio[et] = t
                last_events[(et, etm)] = {'t': t, 'named': False}
                # a priority event may be narrated slightly AFTER its moment (the slot
                # under earlier audio is taken) — real booths call it past-tense
                slot = max(v['t_det'], placed_end + 0.1)
                fact = f"event: {et}" + (f" ({etm})" if etm else '')
                if slot - v['t_det'] > 1.5:
                    fact += " — happened a few seconds ago; call it now, past tense"
                c0 = time.monotonic()
                line = B.chooser(v['t_det'], None, fact, None, recent,
                                 B.broadcaster_names_near(v['t_det']), 'high', None, False)
                cms = int((time.monotonic() - c0) * 1000)
                if line and line.upper() != 'NO_CALL' and len(line.split()) >= 2:
                    rec = {'src': 'blend', 'text': line, 'real_phrase': None,
                           'vision': fact, 'tracker': None, 'stage': 'priority',
                           'vision_latency_ms': v['latency_ms'],
                           'video_time_s': round(slot, 2)}
                    emit(rec, t, slot, max(1.4, len(line.split()) / 2.6), 2.0,
                         t - v['t_det'], cms)
                    time.sleep(0.02); continue

            # --- (2) vision-grounded line from the LATEST ARRIVED detection ---
            if t >= booth:
                if FAST_PROFILE and any(-4.0 <= tt2 - t <= 2.5 and tt2 not in used_son
                                        for tt2, _ in son_sorted):
                    # a real phrase started recently / starts soon — keep its slot clear
                    booth = t + 0.8; time.sleep(0.02); continue
                with VIS_LOCK:
                    # stale-skip: never speak a detection too old to land within
                    # the buffer after chooser+TTS (~3s pipeline remainder)
                    fresh = [v for v in VIS_LIVE
                             if v['t_det'] > vis_consumed and t - v['t_det'] <= STALE_S
                             and v['t_det'] >= placed_end - 0.2]
                    cand = max(fresh, key=lambda v: v['t_det']) if fresh else None
                if cand:
                    vis_consumed = cand['t_det']
                    t_det = cand['t_det']
                    vsig = B.vision_signal(cand['det'])
                    trk_det = None
                    for tt, r in reversed(trk_sorted):
                        if tt <= t - TRK_LAG and abs(tt - t_det) <= 2.0:
                            trk_det = r.get('detection'); break
                    ttruth = B.tracker_truth(trk_det)
                    # FIX-A: priority-class events (goal/cards/penalty) are ONLY speakable
                    # via the corroborated priority block — never through this path
                    if vsig.get('event') in PRIORITY_EVENTS:
                        vsig = {**vsig, 'event': None, 'event_team': None, 'event_conf': None}
                    if FAST_PROFILE:
                        trk_team = B.TEAM.get((trk_det.get('possession') or {}).get('team')) if trk_det else None
                        if vsig.get('poss_team') and trk_team and vsig['poss_team'] != trk_team:
                            vsig = {**vsig, 'poss_team': None, 'poss_player': None, 'poss_pos': None}
                        if vsig.get('poss_player') and vsig.get('poss_conf') != 'high':
                            vsig = {**vsig, 'poss_player': None, 'poss_pos': None}
                    vfact = B.fact_str(vsig)
                    if vsig.get('poss_team') and vfact:
                        lf = last_form_used.get(vsig['poss_team'])
                        if lf:
                            alts = [f for f in TEAM_FORMS[vsig['poss_team']] if f.lower() != lf.lower()]
                            vfact += (f"  [vary the team reference — last line said {lf!r}; "
                                      f"use one of: {', '.join(alts)}]")
                    if (vsig.get('poss_team') and last_team_spoken
                            and vsig['poss_team'] != last_team_spoken and vfact):
                        vfact += (f"  [possession has FLIPPED from {last_team_spoken} to "
                                  f"{vsig['poss_team']} since your last line — mark the transition explicitly]")
                    subj = (vsig.get('poss_team'), vsig.get('poss_player'))
                    speak, gate, scene = False, 4.0, False
                    ev_key = (vsig.get('event'), vsig.get('event_team'))
                    prev_ev = last_events.get(ev_key) if vsig.get('event') else None
                    ev_new_info = bool(vsig.get('poss_player'))
                    ev_ok = vsig.get('event') and (
                        prev_ev is None or t - prev_ev['t'] > 25.0            # R3 window
                        or (ev_new_info and not prev_ev['named']))            # new-info escape
                    if vsig.get('event') in PRIORITY_EVENTS and t - last_prio.get(vsig['event'], -99.0) <= 30.0:
                        ev_ok = False   # F5: card/goal-class dedup is TYPE-only (team labels flap)
                    if vsig.get('event') and not ev_ok:
                        # FIX-B: a deduped event must not linger in the fact the stage narrates
                        vsig = {**vsig, 'event': None, 'event_team': None, 'event_conf': None}
                        vfact = B.fact_str(vsig)
                    if ev_ok:
                        speak, gate = True, 2.5
                    elif vsig.get('poss_player'):
                        speak, gate = True, 2.5
                    elif (vsig.get('poss_team') or ttruth) and t - last_lull > K['lull']:
                        speak, gate, last_lull = True, K['poss_gate'], t
                    elif (t - last_scene > K['scene']
                          and t_det - (lines[-1]['video_time_s'] if lines else -99) >= 15.0):
                        speak, gate, scene, last_scene = True, 4.0, True, t   # R2: >=15s silence AT PLACEMENT
                    if speak:
                        cur_named = vsig.get('poss_player')
                        received = cur_named if (cur_named and cur_named != last_named) else None
                        c0 = time.monotonic()
                        stage = MODE
                        if MODE == 'eager':
                            # separate final stage: windowed observations + timed history,
                            # HEDGED with the fast safe chooser — if the eager line can't
                            # be ready inside the sync budget, revert to the safe line
                            # instead of dropping. Coverage floor = safe mode.
                            with VIS_LOCK:
                                win = [v for v in VIS_LIVE if t_det - 6.0 <= v['t_det'] <= t_det]
                            window = []
                            for v in sorted(win, key=lambda v: v['t_det']):
                                wsig = B.vision_signal(v['det'])
                                if wsig.get('event') in PRIORITY_EVENTS:
                                    # FIX-D: goal/card facts are only speakable via the
                                    # corroborated priority block — never via the window
                                    wsig = {**wsig, 'event': None, 'event_team': None, 'event_conf': None}
                                f = B.fact_str(wsig)
                                if f:
                                    window.append((v['t_det'] - t_det, f))
                            recent_timed = [(l['video_time_s'], l['text']) for l in lines
                                            if not l.get('dropped')]
                            if FAST_PROFILE:
                                # 6s: single fast MENU chooser (~1.0s) — the windowed
                                # prompt costs +0.6s on mini and crowds the budget
                                stage = 'fast'
                                try:
                                    line = B.chooser(t_det, None, vfact, ttruth, recent,
                                                     B.broadcaster_names_near(t_det),
                                                     B.vision_conf(vsig), received, scene)
                                except Exception:
                                    line = 'NO_CALL'
                            else:
                                fe = chooser_pool.submit(
                                    eager_commentator, t_det, window, ttruth, recent_timed,
                                    B.broadcaster_names_near(t_det), received,
                                    B.recent_verbs(recent), t - t_det)
                                fm = chooser_pool.submit(
                                    B.chooser, t_det, None, vfact, ttruth, recent,
                                    B.broadcaster_names_near(t_det), B.vision_conf(vsig),
                                    received, scene)
                                # budget: buffer minus age already spent, minus TTS+lag+margin
                                budget = max(0.8, BUFFER_S - (t - t_det) - 2.5)
                                try:
                                    line = fe.result(timeout=budget)
                                except Exception:
                                    stage = 'safe_fallback'
                                    try:
                                        line = fm.result(timeout=2.0)
                                    except Exception:
                                        line = 'NO_CALL'
                        else:
                            line = B.chooser(t_det, None, vfact, ttruth, recent,
                                             B.broadcaster_names_near(t_det),
                                             B.vision_conf(vsig), received, scene)
                        chooser_ms = int((time.monotonic() - c0) * 1000)
                        # R11 post-check: if this line LEADS with the same team-form as the
                        # previous line, swap it for an unused approved alternative
                        if line and line.upper() != 'NO_CALL':
                            def lead_form(txt):
                                best = None
                                for team, forms in TEAM_FORMS.items():
                                    for fm in forms:
                                        m = _re4.search(r'\b' + _re4.escape(fm) + r'\b', txt, _re4.I)
                                        if m and (best is None or m.start() < best[2]):
                                            best = (team, fm, m.start())
                                return best
                            cur = lead_form(line)
                            prev_lines = [x for x in lines if x['src'] == 'blend' and not x.get('dropped')]
                            prv = lead_form(prev_lines[-1]['text']) if prev_lines else None
                            if cur and prv and cur[0] == prv[0] and cur[1].lower() == prv[1].lower():
                                alts = [f for f in TEAM_FORMS[cur[0]] if f.lower() != cur[1].lower()]
                                if alts:
                                    rep = alts[len(prev_lines) % len(alts)]
                                    if cur[2] == 0:
                                        rep = rep[0].upper() + rep[1:]
                                    line = line[:cur[2]] + rep + line[cur[2] + len(cur[1]):]
                        # R2 post-check: stock-filler phrasing needs >=15s of real silence
                        FILLER_RX2 = _re4.compile(r'quiet spell|midfield battle continues|still all square', _re4.I)
                        if (line and line.upper() != 'NO_CALL' and FILLER_RX2.search(line)
                                and lines and t_det - lines[-1]['video_time_s'] < 15.0):
                            print(f"  [ skip ] R2 filler too close: {line!r}")
                            line = 'NO_CALL'
                        # R4 post-check: if this line flips the spoken team without a
                        # transition marker, retry once with an explicit instruction; if
                        # the retry still violates, skip (silence beats confusion).
                        if line and line.upper() != 'NO_CALL':
                            m_new = POSS_RX.search(line)
                            if (m_new and last_team_spoken and m_new.group(1) != last_team_spoken
                                    and not TRANS_RX.search(line)):
                                line2 = B.chooser(t_det, None,
                                                  (vfact or '') + f" [MUST mark the change of possession from {last_team_spoken} to {m_new.group(1)} — e.g. 'win it back', 'turned over']",
                                                  ttruth, recent, B.broadcaster_names_near(t_det),
                                                  B.vision_conf(vsig), received, scene)
                                m2 = POSS_RX.search(line2 or '')
                                if line2 and line2.upper() != 'NO_CALL' and (
                                        not m2 or m2.group(1) == last_team_spoken or TRANS_RX.search(line2)):
                                    line = line2
                                else:
                                    print(f"  [ skip ] R4 unmarked flip suppressed: {line!r}")
                                    line = 'NO_CALL'
                        if (line and line.upper() != 'NO_CALL' and len(line.split()) >= 2
                                and not B.too_similar(line, recent[-8:])):
                            if vsig.get('event'):
                                last_events[(vsig['event'], vsig.get('event_team'))] = {
                                    't': t, 'named': bool(vsig.get('poss_player'))}
                                if vsig['event'] in PRIORITY_EVENTS:
                                    last_prio[vsig['event']] = t
                            if cur_named:
                                last_subj = (subj, t); last_named = cur_named
                            mts = POSS_RX.search(line)
                            for team, forms in TEAM_FORMS.items():
                                for fm in sorted(forms, key=len, reverse=True):
                                    if _re4.search(r'\b' + _re4.escape(fm) + r'\b', line, _re4.I):
                                        last_form_used[team] = fm
                                        break
                            if mts:
                                last_team_spoken = mts.group(1)
                            elif vsig.get('poss_team'):
                                last_team_spoken = vsig['poss_team']
                            rec = {'src': 'blend', 'text': line, 'real_phrase': None,
                                   'vision': vfact, 'tracker': ttruth,
                                   'stage': stage,
                                   'vision_latency_ms': cand['latency_ms'],
                                   'video_time_s': round(t_det, 2)}
                            emit(rec, t, t_det, max(1.4, len(line.split()) / 2.6), gate,
                                 t - t_det, chooser_ms)
                        else:
                            booth = t + K['retreat']
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
    for lang in ('en', 'fr', 'pt'):
        with wave.open(str(BASE / f'ai_blend_live_{lang}{SUFFIX}_track.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(bytes(audio[lang][:int(B.DURATION_S * SR * 2)]))

    (BASE / f'stt_sanity{SUFFIX}.json').write_text(json.dumps(_SANE_CACHE, indent=1))
    with VIS_LOCK:
        (BASE / f'vis_detections{SUFFIX}.jsonl').write_text(
            '\n'.join(json.dumps({'t_det': v['t_det'], 'latency_ms': v['latency_ms'],
                                   'det': v['det']}) for v in VIS_LIVE) + '\n')
    kept = [l for l in lines if not l.get('dropped')]
    dropped = [l for l in lines if l.get('dropped')]
    kept.sort(key=lambda l: l['video_time_s'])
    (BASE / f'commentary_blend_live{SUFFIX}.jsonl').write_text(
        '\n'.join(json.dumps(l, ensure_ascii=False) for l in kept) + '\n')

    # ---- latency + sync report ----
    vl = sorted(VIS_STATS)
    bl = sorted(l['lat']['behind_live_s'] for l in kept if 'lat' in l)
    shifts = [l['lat'].get('audio_shift_s', 0) for l in kept
              if l.get('lat', {}).get('audio_shift_s', 0) > 0]
    rep = {
        'mode': MODE, 'fixed_delay_s': BUFFER_S, 'policy': 'on-play or dropped — lines never slip',
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
        'fr_track_missing': sum(1 for l in kept if 'fr' in (l.get('lat', {}).get('missing_tracks') or [])),
        'pt_track_missing': sum(1 for l in kept if 'pt' in (l.get('lat', {}).get('missing_tracks') or [])),
        'dropped_texts': [d['text'] for d in dropped],
    }
    (BASE / f'latency_report{SUFFIX}.json').write_text(json.dumps(rep, indent=2))
    ns = sum(1 for l in kept if l['src'] == 'soniox')
    print(f"\n=== TRUE LIVE (fixed {BUFFER_S:.0f}s delay): {len(kept)} on-play lines "
          f"({ns} STT, {len(kept)-ns} vision), {len(dropped)} dropped ===")
    print(json.dumps(rep, indent=2))


if __name__ == '__main__':
    main()

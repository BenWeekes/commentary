#!/usr/bin/env python3
"""Live-SRT BLENDED commentary — the fact+phrase chooser over the real feed.

ffmpeg -re streams the game over SRT; we extract frames; and per booth-free
burst a CHOOSER LLM (given full match context — teams + roster WITH positions)
picks/blends ONE line from a menu of GROUNDED options for that moment:
  - a real short Soniox utterance (verbatim, if one lands here)
  - the vision detector's fact (possession / event, player named via roster)
  - the tracker's objective truth (ball third, team counts)
It uses ONLY the menu (no hallucination), prefers real phrases, names players,
varies verbs vs recent lines, and can NO_CALL. TTS is live; audio is placed by
video-time so the muxed track is synced to the actual game.

Grounding inputs are the validated r1 artifacts for THIS game (events_gpt55 /
events_tracker / soniox_short) looked up by video-time. Live parts: SRT ingest,
the chooser, TTS. Output: ai_blend_live_en_track.wav + commentary_blend_live.jsonl.

Usage: .venv/bin/python run_blend_live.py
"""
from __future__ import annotations
import base64, json, os, re, subprocess, sys, time, wave
import urllib.request, threading
from pathlib import Path

for _l in open('/home/ubuntu/commentary/.env'):
    _l = _l.strip()
    if _l and not _l.startswith('#') and '=' in _l:
        k, _, v = _l.partition('='); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
sys.path.insert(0, str(BASE))
from run_v5 import build_match_context

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
EL_KEY = os.environ['ELEVENLABS_API_KEY']
EL_MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_flash_v2_5')
EN_VOICE = 'gU0LNdkMOQCOrPrwtbee'
SR = 16000
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
NATURAL_LAG_S = 0.3
DURATION_S = 300.0
SOURCE_MP4 = Path('/tmp/v2v_compare/slice_5min.mp4')
SRT_PORT = 10095
SRT_URL_SEND = f"srt://127.0.0.1:{SRT_PORT}?mode=listener&latency=200"
SRT_URL_RECV = f"srt://127.0.0.1:{SRT_PORT}?mode=caller&latency=200"
FRAMES_DIR = Path('/tmp/live_frames_blend'); FRAMES_DIR.mkdir(exist_ok=True)
CHOOSER_MODEL = 'gpt-5.4-mini'
TEAM = {'home': 'Mainz', 'away': 'Union'}
THIRD = {'home_defensive': 'in their own third', 'middle': 'in midfield', 'home_attacking': 'in the final third'}
# match clock for THIS slice (read off the broadcast scoreboard: 76:50, M05 1-1 FCU)
MATCH_CLOCK = "second half, around the 77th minute, level at 1-1, about 13 minutes of normal time left"
CLOCK_START_MIN = 76 + 50 / 60.0     # scoreboard at video t=0

def match_clock_at(t):
    """Clock grounded in VIDEO TIME — the static MATCH_CLOCK drifts ~5 min over the
    slice (reviewer-flagged: '13 minutes left' spoken when ~10 remained)."""
    m = CLOCK_START_MIN + t / 60.0
    left = max(0, 90 - m)
    return (f"second half, {int(m)}th minute, level at 1-1, "
            f"about {max(1, round(left))} minutes of normal time left")

# ---- grounding inputs (looked up by video-time) ----
def load(f):
    p = BASE / f
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []

def by_time(recs, key='video_time_s'):
    return sorted(((round(float(r[key]), 2), r) for r in recs if key in r))

VIS = by_time([r for r in load('events_gpt55.jsonl') if 'detection' in r])
TRK = by_time([r for r in load('events_tracker.jsonl') if 'detection' in r])
SON = by_time(load(os.environ.get('SONIOX_POOL', 'soniox_short.jsonl')))
SON_STARTS = [tt for tt, _ in SON]
CTX = build_match_context()
# AUTHORITATIVE player identity from the Sportradar lineup.
# Keyed by (team, number) — shirt numbers COLLIDE across teams (11 collisions in this
# match; a number-only key silently dropped 11 of 40 players and misattributed the
# rest). Within-team duplicate numbers are AMBIGUOUS and excluded from naming
# (fail closed). NUM_TEAMS maps a number to the set of teams that use it, so a
# team-less sighting can still name a player when the number is globally unique.
_sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
LINEUP = {}                 # (team, number) -> {name,pos,team,starter}
NUM_TEAMS = {}              # number -> set of teams
_AMBIG = set()              # (team, number) seen more than once -> never name from it
ALL_PLAYERS = []            # every roster entry (survives number collisions) — for surname grounding
for _c in _sr['lineups']['lineups']['competitors']:
    _tm = 'Mainz' if 'Mainz' in _c.get('name', '') else 'Union'
    for _p in _c.get('players', []):
        _nm = _p.get('name', ''); _sur = _nm.split(',')[0].strip() if ',' in _nm else _nm
        _k = (_tm, str(_p.get('jersey_number')))
        if _k in LINEUP:
            _AMBIG.add(_k)
        LINEUP[_k] = {
            'name': _sur, 'pos': (_p.get('position') or _p.get('type') or '').replace('_', ' '),
            'team': _tm, 'starter': bool(_p.get('starter'))}
        NUM_TEAMS.setdefault(_k[1], set()).add(_tm)
        ALL_PLAYERS.append({'name': _sur, 'team': _tm})

def player_by_number(num, team=None):
    """Roster lookup that fails closed: returns the player dict only when the
    (team, number) pair is unambiguous. team=None resolves only globally-unique numbers."""
    n = str(num)
    if team is None:
        teams = NUM_TEAMS.get(n) or set()
        if len(teams) != 1:
            return None
        team = next(iter(teams))
    k = (team, n)
    if k in _AMBIG:
        return None
    return LINEUP.get(k)

ROSTER_BLOCK = "\n".join(f"  #{k[1]} {v['name']} ({v['team']}, {v['pos']})"
                         for k, v in sorted(LINEUP.items(),
                                            key=lambda kv: (kv[1]['team'], int(kv[0][1]) if kv[0][1].isdigit() else 99)))
# broadcaster player mentions = ground-truth identity per moment (from the real commentary)
_SUR = {p['name'] for p in ALL_PLAYERS}
MENTIONS = [(float(_g.get('start_s', 0)), _w)
            for _g in load('gold_soniox_5min.jsonl')
            for _w in re.findall(r"[A-Za-z][A-Za-z'-]+", _g.get('text', '')) if _w in _SUR]

def broadcaster_names_near(t, w=8.0):
    return sorted({s for tt, s in MENTIONS if abs(tt - t) <= w})

def nearest(idx, t, w=1.4):
    best = None; bd = w
    for tt, r in idx:
        d = abs(tt - t)
        if d <= bd: bd = d; best = r
    return best

def pop_soniox(t, used, w=1.6):
    for tt, r in SON:
        if tt in used: continue
        if abs(tt - t) <= w:
            used.add(tt); return r
    return None

USE_CONF = ('high', 'medium')   # blend uses vision only when medium+ ; low is dropped

def vision_signal(det):
    """Structured signal from a detection, CONFIDENCE-TIERED:
    - only surface events/possession the detector rated high or medium (drop low)
    - name a SPECIFIC player only at HIGH confidence; medium stays team-level."""
    if not det: return {}
    ev = evteam = evconf = None
    for e in det.get('events') or []:
        et, ec = e.get('type'), e.get('confidence')
        if et and et not in ('replay_starts', 'replay_ends') and ec in USE_CONF:
            ev, evteam, evconf = et, TEAM.get(e.get('team')), ec; break
    p = det.get('possession') or {}
    pconf = p.get('confidence')
    pteam = pname = ppos = None
    if pconf in USE_CONF:
        pteam = TEAM.get(p.get('team'))
        num = p.get('player_shirt_number')
        # name a real player only when (team, number) resolves UNAMBIGUOUSLY in the roster:
        # team known -> exact (team, number) lookup; team unknown -> only a globally-unique
        # number may name. Colliding/duplicate numbers fail closed (stay team-level).
        li = player_by_number(num, pteam) if num is not None else None
        if li:
            pname, ppos = li['name'], li['pos']
    return {'event': ev, 'event_team': evteam, 'event_conf': evconf,
            'poss_team': pteam, 'poss_player': pname, 'poss_pos': ppos, 'poss_conf': pconf}

def fact_str(sig):
    if sig.get('event'):
        return f"event: {sig['event']}" + (f" ({sig['event_team']})" if sig['event_team'] else '')
    if sig.get('poss_team'):
        who = f", player {sig['poss_player']} ({sig.get('poss_pos')})" if sig.get('poss_player') else ""
        return f"possession: {sig['poss_team']}{who}"
    return None

def vision_conf(sig):
    """The confidence tier of whatever signal fact_str will speak (event first)."""
    return sig.get('event_conf') if sig.get('event') else sig.get('poss_conf')

def too_similar(line, recent):
    """True if a generated line nearly repeats a recent one (word-set overlap)."""
    lw = set(re.sub(r'[^a-z ]', ' ', line.lower()).split())
    if not lw: return False
    for rt in recent:
        rw = set(re.sub(r'[^a-z ]', ' ', (rt or '').lower()).split())
        if rw and len(lw & rw) / len(lw | rw) >= 0.55:
            return True
    return False

def tracker_truth(det):
    """Objective location + shape — phrased for a broadcaster (no camera/detector talk)."""
    if not det: return None
    tr = det.get('tracker') or {}
    bits = []
    if tr.get('ball_third'): bits.append('ball ' + THIRD.get(tr['ball_third'], tr['ball_third']))
    m, u = tr.get('mainz', 0), tr.get('union', 0)
    if m + u >= 7 and abs(m - u) >= 3:
        bits.append(('Mainz' if m > u else 'Union') + ' have numbers back')
    return '; '.join(bits) or None

CHOOSER_SYSTEM = f"""You are the single live commentary voice for a Bundesliga match:
Mainz (red, home) vs Union Berlin (olive, away) at the Mewa Arena. Referee: Florian Exner.
MATCH CLOCK: {MATCH_CLOCK}. You may reference this (the half, the minute, the 1-1
scoreline, time remaining) — it is TRUE. Use it SPARINGLY and VARY it: mention the clock
OR the territory OR the atmosphere OR the phase — do NOT repeat "level at 1-1" over and
over. At most one scoreline reference every couple of minutes.

For THIS moment you get a MENU of GROUNDED options from different sources. Output ONE
short spoken line (4-10 words) OR exactly NO_CALL.

HARD RULES:
- Use ONLY the information in the MENU plus the ROSTER. Do NOT invent events, players,
  shots, goals, or the scoreline.
- SOURCE PRIORITY: if a REAL PHRASE (STT) is provided, use it (verbatim or lightly adapted) —
  it is the real broadcaster and always wins. Only fall back to the VISION signal when there
  is no real phrase.
- NAMING: you may name a specific player ONLY when the menu hands you one — the vision fact names
  a player (its shirt number was clearly read and roster-matched) OR the broadcaster just named
  someone. Those names are safe at either confidence. When the menu gives NO name, say something
  else — team/role possession, the event, territory, or a scene note — but never GUESS a name.
- PASS RECEIVED: when the menu says a pass was just received by a named player, the most natural
  call is to name the receiver — "To Khedira." / "Khedira collects it." / "picked out Amiri."
  Use the surname. This is a winning, authentic play-by-play line.
- STYLE: when there is no real_phrase, you ARE the commentator — generate a natural, live
  play-by-play line in authentic broadcast style from the vision/tracker facts. Short, punchy,
  present tense. This is exactly what a TV commentator says over this passage of play.
- PLAYER NAMES: only ever use a name from the ROSTER below. If the MENU names a specific
  player, use them. Prefer a player the BROADCASTER just named (given in the menu) — that is
  ground truth. If the ball-carrier is NOT clearly identified, name the TEAM or a role
  ("a Mainz midfielder pushes on") — NEVER guess a specific name.
- The TRACKER line is objective ground truth for location/shape — trust it over any guess.
- Staying on the SAME topic or player is FINE when that is what is happening — passages of
  play are normal. What you must NOT do is reuse the same VERB or opener as the recent lines.
  The menu lists AVOID VERBS (already used recently) — pick a different verb. Only output
  NO_CALL when there is genuinely nothing to say, NOT merely because the subject is unchanged.
- Speak like a real broadcaster: NEVER mention the camera, "on screen", "on the screen",
  the detector/tracker, or say "nothing certain". If the moment is unclear, output NO_CALL.

ROSTER (number, name, team, position):
{ROSTER_BLOCK}
Output only the line, or NO_CALL."""

# commentary verbs we track for over-use — same topic is fine, a repeated VERB is not
VERB_LEXICON = {
    'plays', 'passes', 'moves', 'carries', 'brings', 'takes', 'drives', 'drills', 'slots',
    'whips', 'threads', 'clips', 'lifts', 'feeds', 'slides', 'curls', 'wins', 'breaks',
    'surges', 'pushes', 'sends', 'swings', 'floats', 'delivers', 'clears', 'heads', 'nods',
    'controls', 'receives', 'collects', 'works', 'switches', 'spreads', 'releases', 'keeps',
    'holds', 'shields', 'turns', 'runs', 'strides', 'knocks', 'rolls', 'chips', 'crosses',
    'shoots', 'strikes', 'fires', 'tackles', 'wins', 'presses', 'closes', 'blocks', 'flicks',
    'picks', 'finds', 'looks', 'goes', 'digs', 'weaves', 'bursts', 'jockeys', 'tracks',
}

def recent_verbs(recent, n=6):
    used = set()
    for line in recent[-n:]:
        for w in re.sub(r'[^a-z ]', ' ', (line or '').lower()).split():
            if w in VERB_LEXICON:
                used.add(w)
    return sorted(used)

def chooser(t, real_phrase, vfact, ttruth, recent, bnames=None, vconf=None, received=None, scene=False):
    avoid = recent_verbs(recent)
    menu = [f"- real_phrase: {real_phrase!r}" if real_phrase else "- real_phrase: (none)",
            f"- vision: {vfact}  (confidence: {vconf})" if vfact else "- vision: (nothing certain)",
            f"- tracker(truth): {ttruth}" if ttruth else "- tracker(truth): (no read)",
            f"- pass just received by: {received}" if received else "- pass just received by: (nobody new)",
            f"- match clock: {match_clock_at(t)}",
            ("- SCENE: quiet passage — a brief clock/score/atmosphere line is welcome here"
             if scene else "- SCENE: (normal play)"),
            f"- broadcaster just named: {', '.join(bnames)}" if bnames else "- broadcaster just named: (nobody)",
            f"- AVOID VERBS (used recently, pick a different one): {', '.join(avoid)}" if avoid
            else "- AVOID VERBS: (none yet)"]
    rc = "\n".join(f"  - {r}" for r in recent[-12:]) or "  - none"
    user = f"MOMENT t={t:.0f}s\nMENU:\n" + "\n".join(menu) + f"\nRECENT LINES (same topic ok; do NOT reuse their verbs):\n{rc}\nLine:"
    try:
        r = client.responses.create(model=CHOOSER_MODEL, instructions=CHOOSER_SYSTEM,
                                    input=[{"role": "user", "content": user}], max_output_tokens=40)
        return re.sub(r'\s+', ' ', (r.output_text or '').strip().strip('"'))
    except Exception as e:
        print(f"  chooser err: {e}"); return 'NO_CALL'

FR_VOICE = 'LcKoSBj8CeBInl4bQHtq'
PT_VOICE = 'HR2TRGmi4QbMsO5omv7l'   # production Brazilian voice (docs/ai/L1/04_conventions.md)
TRANSLATE_PT_SYSTEM = (
    "You are the Brazilian Portuguese LOCALIZER for live TV football commentary. "
    "Rewrite the English line as a Brazilian football commentator would actually SAY it "
    "on air - natural futebol register, not a literal translation. Same meaning, same "
    "length or shorter. Keep player and team names exactly.\n\n"
    "GLOSSARY (reviewer-maintained - preferred forms):\n"
    "- final third -> 'ultimo terco' is NEVER said -> 'entrada da area' / 'campo de ataque'\n"
    "- free kick -> 'falta' (cobranca de falta); corner -> 'escanteio'\n"
    "- possession colour -> 'troca passes', 'toca a bola', 'cadencia o jogo'\n"
    "- numbers back / compact -> 'fechada atras', 'retranca' (only when truly parked)\n"
    "- quiet spell -> 'jogo morno' / 'momento de estudo'\n"
    "- avoid European-PT forms; this is pt-BR broadcast speech.\n"
    "If the English line is nonsensical as football speech, output the closest sensible "
    "pt-BR football line rather than a literal rendering. Return only the Portuguese line.")


def translate_pt(text):
    """Returns the pt-BR line, or None on failure — the caller must treat None as a
    MISSING track (silent + logged), never speak the English fallback on the PT track."""
    try:
        r = client.responses.create(model=CHOOSER_MODEL, instructions=TRANSLATE_PT_SYSTEM,
                                    input=[{"role": "user", "content": text}], max_output_tokens=200)
        out = re.sub(r'\s+', ' ', (r.output_text or '').strip().strip('"'))
        return out or None
    except Exception:
        return None
TRANSLATE_SYSTEM = (
    "You are the FRENCH LOCALIZER for live TV football commentary. Rewrite the English line as a "
    "French football commentator would actually SAY it on air — natural broadcast register, not a "
    "literal translation. Same meaning, same length or shorter. Keep PLAYER names exactly; standard "
    "French team exonyms are fine (e.g. Mayence for Mainz).\n\n"
    "GLOSSARY (reviewer-maintained — banned calque -> preferred form):\n"
    "- 'le dernier tiers' is NEVER said -> 'les trente derniers mètres' / 'le camp adverse'\n"
    "- 'sonder' is not football French -> 'tenter' / 'essayer'\n"
    "- 'moment calme' -> 'temps faible'\n"
    "- players returning to position -> 'sont revenus'\n"
    "- a move that breaks down / doesn't come off -> 'l'action ne passe pas' / 'ça ne passe pas' "
    "(NEVER 'ça se casse', 'ça se termine là')\n"
    "- a player's PASS -> 'la passe de X' (when it is a pass; do NOT say 'le ballon de X' for a pass)\n"
    "- naming a player found on the ball -> 'On retrouve X' / 'Et X' (avoid 'Et voici tout simplement X')\n"
    "- ball played INTO the box -> 'dans la surface'; the six-yard area -> 'les six mètres'\n"
    "- possession colour, keep idiomatic: 'conserve le ballon', 'fait tourner', 'ressort proprement'\n"
    "- 'X à la place de Y' means a SUBSTITUTION — never use it for a pass; a pass is "
    "'X pour Y' / 'la passe de X'\n"
    "- a quiet passage -> 'temps calme' (never bare 'Calme' as an opener)\n"
    "- introducing a player on the ball -> 'Et voici X' / 'On retrouve X' (not 'C'est aussi X')\n"
    "- vary attacking-third references: 'les trente derniers metres' / 'le camp adverse' / "
    "'aux abords de la surface' — never the same form twice in a row\n"
    "- avoid over-literal calques; if the English is nonsensical as football speech, output the "
    "closest sensible French football line rather than a literal rendering.\n"
    "Return only the French line.")

def translate_fr(text):
    """Returns the French line, or None on failure — see translate_pt."""
    try:
        r = client.responses.create(model=CHOOSER_MODEL, instructions=TRANSLATE_SYSTEM,
                                    input=[{"role": "user", "content": text}], max_output_tokens=90)
        out = re.sub(r'\s+', ' ', (r.output_text or '').strip().strip('"'))
        return out or None
    except Exception:
        return None

def tts(text, voice):
    body = json.dumps({"text": text, "model_id": EL_MODEL,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
                                 data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def start_sender():
    return subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','warning','-re','-i',str(SOURCE_MP4),
        '-c:v','copy','-c:a','copy','-f','mpegts',SRT_URL_SEND], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

def start_receiver():
    return subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','warning','-i',SRT_URL_RECV,
        '-vf',f'fps=1/{SAMPLE_INTERVAL_S},scale=1280:720','-q:v','4','-start_number','1','-y',
        str(FRAMES_DIR/'f_%05d.jpg')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main():
    for f in FRAMES_DIR.glob('f_*.jpg'): f.unlink()
    print(f"BLEND live: {len(VIS)} vision, {len(TRK)} tracker, {len(SON)} soniox phrases; roster {len(CTX['roster'])}")
    sender = start_sender(); time.sleep(1.5); recv = start_receiver()
    audio_en = bytearray(int((DURATION_S + 30) * SR * 2))
    audio_fr = bytearray(int((DURATION_S + 30) * SR * 2))
    lines = []; used_son = set(); recent = []
    booth_video = 0.0; speaking_until = 0.0; processed = 0; wall0 = time.monotonic()
    last_new = time.monotonic(); stopping = False
    last_event = (None, -99.0)   # (event_type, time) — no re-narrating same event within 8s
    last_subj = (None, -99.0)    # ((team,player), time) — same for possession subject
    last_lull = -99.0            # last "lull colour" line — keep those sparse
    last_named = None            # last named ball-carrier — a NEW one means a pass was received
    last_scene = -99.0           # last clock/atmosphere scene-set — keep sparse

    audio_end = {'en': 0, 'fr': 0}   # last written byte per track — placement floor
    audio_lock = threading.Lock()

    def place(rec, t):
        # No-clobber placement: consecutive real phrases can sit closer together
        # than our TTS takes to speak them; never overwrite the previous line's
        # tail — shift this line to start after it instead.
        en = tts(rec['text'], EN_VOICE)
        fr = translate_fr(rec['text']); rec['fr'] = fr
        frp = tts(fr, FR_VOICE)
        with audio_lock:
            for lang, buf, pcm in (('en', audio_en, en), ('fr', audio_fr, frp)):
                b = int((t + NATURAL_LAG_S) * SR) * 2
                if b < audio_end[lang]:
                    b = audio_end[lang]
                b -= b % 2
                u = min(len(pcm), len(buf) - b)
                if u > 0:
                    buf[b:b+u] = pcm[:u]
                    audio_end[lang] = b + u

    def emit(rec, t, est, gate):
        lines.append(rec); recent.append(rec['text'])
        threading.Thread(target=place, args=(rec, t), daemon=True).start()
        print(f"  [{t:6.1f}s] ({rec['src']}) {rec['text']}")
        return t + NATURAL_LAG_S + est + gate

    while True:
        frames = sorted(FRAMES_DIR.glob('f_*.jpg')); n = len(frames)
        if n > processed: last_new = time.monotonic()
        processed = n
        if sender.poll() is not None and time.monotonic() - last_new > 5:
            stopping = True
        if n >= CONTEXT_FRAMES:
            t = n * SAMPLE_INTERVAL_S  # video-time of newest frame
            # (1) a real Soniox phrase PREEMPTS — human lines always get spoken
            real = pop_soniox(t, used_son)
            if real:
                rt = float(real.get('video_time_s', t))   # place at the phrase's real (gold) time so rows align
                rec = {'video_time_s': round(rt, 2), 'text': real['text'], 'src': 'soniox',
                       'real_phrase': real['text'], 'vision': None, 'tracker': None}
                est = real.get('dur') or max(1.4, len(real['text'].split()) / 2.6)
                booth_video = emit(rec, rt, est, 0.4); speaking_until = rt + NATURAL_LAG_S + est
                time.sleep(0.02); continue
            # (1.5) a FRESH named ball-carrier IN OPEN PLAY may preempt the trailing pause of
            # the previous line (once its audio has finished) — genuine passes get called
            # promptly. Gated on phase=open_play so replays/stoppages never trigger a name.
            _vd = (nearest(VIS, t) or {}).get('detection')
            _vs = vision_signal(_vd)
            named_preempt = bool(_vd and _vd.get('phase') == 'open_play'
                                 and _vs.get('poss_player') and _vs['poss_player'] != last_named
                                 and t >= speaking_until)
            # (2) generated lines — when the booth is free OR a fresh named carrier preempts
            if t >= booth_video or named_preempt:
                if any(0 <= s - t <= 2.5 and s not in used_son for s in SON_STARTS):
                    booth_video = t + 1.0; time.sleep(0.02); continue  # yield to imminent real phrase
                vsig = vision_signal((nearest(VIS, t) or {}).get('detection'))
                vfact = fact_str(vsig)
                ttruth = tracker_truth((nearest(TRK, t) or {}).get('detection'))
                subj = (vsig.get('poss_team'), vsig.get('poss_player'))
                # same subject/topic is FINE (it's the play); verb variety is enforced in the
                # chooser via the avoid-verbs list. We only gate on PACING, not on subject.
                # Verbose pacing — the booth should rarely go quiet. Speak on events,
                # named carriers, possession colour, and (as a last resort) a scene-set,
                # so we don't leave long silences while the play continues.
                speak, gate, scene = False, 4.0, False
                if vsig.get('event') and (vsig['event'] != last_event[0] or t - last_event[1] > 8):
                    speak, gate = True, 2.5                                    # a NEW event
                elif vsig.get('poss_player'):
                    speak, gate = True, 2.5                                    # named ball-carrier (same subject ok)
                elif (vsig.get('poss_team') or ttruth) and t - last_lull > 3:
                    speak, gate, last_lull = True, 3.0, t                      # possession colour
                elif t - last_scene > 40:
                    speak, gate, scene, last_scene = True, 4.0, True, t        # rare dead-air scene note
                if not speak:
                    booth_video = t + 0.8; time.sleep(0.02); continue
                # a NEW named carrier = a pass received → let the LLM say "To <surname>"
                cur_named = vsig.get('poss_player')
                received = cur_named if (cur_named and cur_named != last_named) else None
                line = chooser(t, None, vfact, ttruth, recent, broadcaster_names_near(t),
                               vision_conf(vsig), received, scene)
                if line and line.upper() != 'NO_CALL' and len(line.split()) >= 2 and not too_similar(line, recent[-8:]):
                    if vsig.get('event'): last_event = (vsig['event'], t)
                    if vsig.get('poss_player'): last_subj = (subj, t); last_named = cur_named
                    rec = {'video_time_s': round(t, 2), 'text': line, 'src': 'blend',
                           'real_phrase': None, 'vision': vfact, 'tracker': ttruth}
                    est2 = max(1.4, len(line.split()) / 2.6)
                    booth_video = emit(rec, t, est2, gate); speaking_until = t + NATURAL_LAG_S + est2
                else:
                    booth_video = t + 1.2
        if stopping:
            print("[main] sender ended; draining"); time.sleep(2.0); break
        if time.monotonic() - last_new > 400: break
        time.sleep(0.05)

    for p in (sender, recv):
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill()
    time.sleep(7.0)  # let last translate + EN/FR TTS threads finish
    for lang, buf in (('en', audio_en), ('fr', audio_fr)):
        with wave.open(str(BASE / f'ai_blend_live_{lang}_track.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(bytes(buf[:int(DURATION_S * SR * 2)]))
    (BASE / 'commentary_blend_live.jsonl').write_text(
        '\n'.join(json.dumps(l, ensure_ascii=False) for l in lines) + '\n')
    ns = sum(1 for l in lines if l['src'] == 'soniox')
    print(f"\n=== {len(lines)} lines ({ns} real-phrase, {len(lines)-ns} blended) -> EN + FR tracks ===")


if __name__ == '__main__':
    main()

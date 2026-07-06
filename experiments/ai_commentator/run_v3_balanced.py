#!/usr/bin/env python3
"""AI commentator v3 — calibrated middle between v1 (over-naming) and v2 (name-shy).

Keeps v2's live-pace gate and "don't carry forward names" rule but adds explicit
encouragement to NAME PLAYERS when the shirt number is readable, and lets the
model use pre-game insights when it does name someone.

Outputs ai_commentary_v3.*
"""
from __future__ import annotations
import base64, json, os, re, sys, time, subprocess, wave
import urllib.request, urllib.error
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v3.jsonl'
OUT_KEPT = BASE / 'commentary_v3_kept.txt'
OUT_AUDIO_WAV = BASE / 'ai_commentary_v3_track.wav'
OUT_MP4 = BASE / 'ai_commentary_v3.mp4'
OUT_SBS = BASE / 'ai_commentary_v3_sidebyside.mp4'
OUT_SCHED = BASE / 'commentary_v3_scheduled.jsonl'
SOURCE_MP4 = Path('/tmp/v2v_compare/slice_5min.mp4')

SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.4-mini'
MAX_OUTPUT_TOKENS = 50  # slightly more headroom for insights
TEMPERATURE = 0.55
SR_TTS = 16000
DURATION_S = 300.0
DEDUP_JACCARD = 0.4

EL_KEY = os.environ['ELEVENLABS_API_KEY']
EL_VOICE = os.environ.get('ELEVENLABS_VOICE_ID_EN_SPORTSCASTER') or os.environ['ELEVENLABS_VOICE_ID']
EL_MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_flash_v2_5')

PLAYER_INSIGHTS = {
    'Burke': "In Scotland's World Cup squad picture; Eta gives him longer runs than Baumgart did.",
    'Sieb': "On a two-year loan from Bayern, leaving at season end; received a formal farewell pre-match.",
    'Doekhi': "Dutch centre-back, born in Rotterdam; English clubs watching him.",
    'Weiper': "Tall striker; Germany U21; hasn't had things go his way this campaign.",
    'Kohn': "The 'Derrick' name is a long-running joke with the German detective TV series.",
    'Zentner': "Strong at coming for crosses; one-on-ones not his main strength.",
    'Veratschnig': "Very good recovery pace at the back.",
    'Juranovic': "Coming back from an injury-hit season.",
    'Posch': "Aerial threat — stands in the air well.",
}

MANAGER_INSIGHTS = {
    'Eta': "Marie-Louise Eta, first woman head coach in Bundesliga history; took over from Urs Fischer.",
    'Fischer': "Urs Fischer, former Union manager — title-winning, contribution will never be forgotten.",
}

STYLE_EXAMPLES = [
    "Khedira leading that particular charge.",
    "At first sight, I'm with the referee.",
    "I think it's a race to the ball.",
    "Veratschnig venture forward. Here's Kohr.",
    "Mainz have been using the right-hand side, but they've elected to attack from the left.",
    "Tall striker. Hasn't had things go his way too much in this campaign.",
    "Here at the Mewa Arena.",
    "Wide of the target.",
    "Yeah, home form is always vital, especially for smaller clubs.",
    "Trying to keep Juranovic in check.",
    "He has to be content with a corner.",
    "We're inside the final fifteen here.",
    "Burke given a lengthy run, a feature of Eta's matches.",  # demonstrates using an insight
]


def _parse_roster_text(text):
    out = []
    team = None
    role = 'starter'
    for line in text.splitlines():
        l = line.strip()
        if not l: continue
        if l.endswith('— home:'): team='FSV Mainz'; role='starter'; continue
        if l.endswith('— away:'): team='Union Berlin'; role='starter'; continue
        if 'Starting XI' in l: role='starter'; continue
        if 'Substitutes' in l: role='bench'; continue
        m = re.match(r'#(\S+)\s+(.+)', l)
        if m and team:
            full = m.group(2).strip()
            short = full.split(',')[0].strip() if ',' in full else full
            out.append({'team': team, 'number': m.group(1), 'short_name': short, 'name': full, 'role': role})
    return out


def _load_sr_positions():
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    out = {}
    for c in sr['lineups']['lineups']['competitors']:
        for p in c.get('players', []):
            out[p.get('name', '')] = p.get('position', '').replace('_', ' ')
    return out


def build_match_context():
    roster_text = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/roster.json'))['roster_text']
    roster = _parse_roster_text(roster_text)
    positions = _load_sr_positions()
    for p in roster:
        p['position'] = positions.get(p['name'], '')
        p['insight'] = PLAYER_INSIGHTS.get(p['short_name'], '')
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    formations = {c['name']: c.get('formation', {}).get('type', '?')
                  for c in sr['lineups']['lineups']['competitors']}
    return {
        'title': 'FSV Mainz vs Union Berlin, Bundesliga matchday 33',
        'venue': 'Mewa Arena, Mainz',
        'home_team': 'FSV Mainz', 'home_abbr': 'M05',
        'away_team': 'Union Berlin', 'away_abbr': 'UNI',
        'home_color': 'predominantly white shirts with red trim',
        'away_color': 'predominantly red shirts',
        'home_formation': formations.get('FSV Mainz', '?'),
        'away_formation': formations.get('Union Berlin', '?'),
        'roster': roster,
        'storyline': "Both sides safe in the table but jockeying for league position. Single commentator with analyst booth.",
        'final_score_private': 'M05 1 - 3 UNI',
    }


def _format_roster_block(roster, team_name, abbr):
    rows = [p for p in roster if p['team'] == team_name]
    if not rows: return ''
    lines = [f"{abbr} squad:"]
    for p in rows:
        bits = [f"#{p['number']}", p['short_name']]
        if p['name'] != p['short_name']:
            bits.append(f"({p['name']})")
        meta = [p['role']]
        if p['position']: meta.append(p['position'])
        bits.append(f"[{'/'.join(meta)}]")
        line = "  " + " ".join(bits)
        if p['insight']:
            line += f"\n    pre-game note: {p['insight']}"
        lines.append(line)
    return "\n".join(lines)


def build_match_context_text(ctx):
    parts = [
        f"Match: {ctx['title']}, at {ctx['venue']}.",
        f"Storyline: {ctx['storyline']}",
        f"{ctx['home_team']} ({ctx['home_abbr']}): formation {ctx['home_formation']}, {ctx['home_color']}.",
        f"{ctx['away_team']} ({ctx['away_abbr']}): formation {ctx['away_formation']}, {ctx['away_color']}.",
        f"  Union manager: {MANAGER_INSIGHTS.get('Eta', '')}",
        f"  Union ex-manager context: {MANAGER_INSIGHTS.get('Fischer', '')}",
        _format_roster_block(ctx['roster'], ctx['home_team'], ctx['home_abbr']),
        _format_roster_block(ctx['roster'], ctx['away_team'], ctx['away_abbr']),
    ]
    return "\n".join(parts)


def build_visual_prompt(ctx_text, latest_time_s, previous_calls):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none yet"
    examples = "\n".join(f"  - \"{e}\"" for e in STYLE_EXAMPLES)
    return f"""You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers.

PROFILE: energetic American-English sportscaster. Short, sharp, urgent during
attacks; reflective during lulls; restrained when the picture is unclear.

VIDEO CONTEXT
Current video clock (relative to start of this slice): {latest_time_s:.1f}s.
You see a short burst of frames, oldest first, newest last. Comment on
whatever is happening in the NEWEST frame.

OUTPUT
- 3-12 words, usually one sentence or a fragment.
- Variety: rotate through action calls, named identifications, pass-type
  observations, tactical reads, time references, atmosphere, terse outcomes,
  player-context asides (only when you can use a pre-game note).
- Empty silence is fine. Return "NO_CALL" for unreadable / static / replay /
  pure-crowd shots — but use it sparingly when football is actually visible.
- Bias toward CALLING: when in doubt and there's football on screen, make
  a call. Don't be shy.

NAMING — LEAN INTO IT
- NAME PLAYERS WHENEVER REASONABLE. Strong identification is the heart of
  good football commentary — viewers want to hear who has the ball, who
  passed it, who saved it. Don't hide behind generic descriptions.
- Use the shirt number on the player's back when you can see it. Use kit
  colour + position + roster to pick the most likely name when you can't.
- Occasional misidentifications are acceptable — a wrong name once in a
  while is far less damaging than every line being "the Mainz striker /
  the Union winger". Lean toward naming.
- Goalkeepers are easy: Klaus for Union, Zentner for Mainz. Use the name.
- A subbed-on player will normally be visually different from the player
  they replaced (different number, sometimes different role). If you
  notice an obvious change, switch names accordingly — but don't agonise
  over it.
- DO NOT invent a name that's not on either squad. Stick to the roster
  below.

USING PRE-GAME NOTES
- The squad block below lists pre-game notes for some players. If a NAMED
  player has a note that fits the current moment, briefly weave it in.
  Example: "Burke given a lengthy run again, a feature of Eta's side."
- Do not use a note more than once per session.
- Do not invent notes for players who don't have them.

NO INVENTION
- No goals, cards, scores, substitutions, or events not visibly happening.
- The known final result is private metadata — do not reference it.

STYLE EXAMPLES (illustrative; do NOT transcribe verbatim):
{examples}

CONTEXT
{ctx_text}

RECENT CALLS — do not repeat the same observation:
{previous}

Produce your next call:
"""


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def call_vision(client, burst_paths, latest_time_s, previous_calls, ctx_text):
    content = [{"type": "input_text", "text": build_visual_prompt(ctx_text, latest_time_s, previous_calls)}]
    for p in burst_paths:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    t0 = time.monotonic()
    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": content}],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
        )
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"{type(e).__name__}: {str(e)[:200]}"


def tts_one(text):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}?output_format=pcm_16000"
    body = json.dumps({
        "text": text, "model_id": EL_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": EL_KEY, "Content-Type": "application/json", "Accept": "audio/pcm",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as r:
        pcm = r.read()
    return pcm, int((time.monotonic()-t0)*1000)


NO_CALL_RE = re.compile(r"^\s*(no[_ ]?call|n/a|—|-+)\s*$", re.I)
def is_no_call(text): return bool(NO_CALL_RE.match(text or ''))
def is_repetitive(text, previous, threshold):
    if not text: return False
    nt = set(re.sub(r'\W+', ' ', text.lower()).split())
    for prev in previous[-8:]:
        np = set(re.sub(r'\W+', ' ', prev.lower()).split())
        if not nt or not np: continue
        if len(nt & np) / len(nt | np) >= threshold:
            return True
    return False


def main():
    frame_paths = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Frames: {len(frame_paths)}")
    ctx = build_match_context()
    ctx_text = build_match_context_text(ctx)
    print(f"Context size: {len(ctx_text)} chars, insights: {sum(1 for p in ctx['roster'] if p['insight'])}/{len(ctx['roster'])}")
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]))
    print(f"Bursts: {len(bursts)}")

    accepted = []; all_attempts = []
    booth_busy_until = 0.0
    LIVE_GAP_S = 0.20
    no_call=0; repetitive=0; errors=0; skipped=0

    t_start = time.time()
    last_print = time.time()
    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        if latest_time_s < booth_busy_until + LIVE_GAP_S:
            skipped += 1
            continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        text, vision_ms, err = call_vision(client, burst, latest_time_s, prev_texts, ctx_text)
        attempt = {
            'burst_index': i, 'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': vision_ms, 'text': text,
            'accepted': False, 'reason': None, 'error': err,
        }
        if err:
            errors += 1; attempt['reason']='error'
        elif not text:
            attempt['reason']='empty'
        elif is_no_call(text):
            no_call += 1; attempt['reason']='no_call'
        elif is_repetitive(text, [a['text'] for a in accepted], DEDUP_JACCARD):
            repetitive += 1; attempt['reason']='repetitive'
        else:
            try:
                pcm, tts_ms = tts_one(text)
            except urllib.error.HTTPError as e:
                attempt['reason'] = f'tts_error_{e.code}'
                all_attempts.append(attempt); continue
            duration_s = len(pcm)/2/SR_TTS
            realistic_lag_s = (vision_ms + tts_ms)/1000.0
            scheduled_start_s = latest_time_s + realistic_lag_s
            scheduled_end_s = scheduled_start_s + duration_s
            attempt.update({
                'accepted': True, 'tts_ms': tts_ms,
                'duration_s': round(duration_s, 3),
                'realistic_lag_s': round(realistic_lag_s, 3),
                'scheduled_start_s': round(scheduled_start_s, 3),
                'scheduled_end_s': round(scheduled_end_s, 3),
            })
            accepted.append({**attempt, 'pcm': pcm})
            booth_busy_until = scheduled_end_s
        all_attempts.append(attempt)
        if time.time() - last_print > 10:
            print(f"  burst {burst_idx}/{len(bursts)} t={latest_time_s:.1f}s accepted={len(accepted)} skipped={skipped} elapsed={time.time()-t_start:.0f}s last={(text or '')[:60]!r}")
            last_print = time.time()

    print(f"\nSummary: vision={len(all_attempts)} accepted={len(accepted)} skipped={skipped} no_call={no_call} rep={repetitive} err={errors}")
    if accepted:
        lats_v = sorted(a['vision_latency_ms'] for a in accepted)
        lats_t = sorted(a['tts_ms'] for a in accepted)
        lats_total = sorted(int(a['realistic_lag_s']*1000) for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision p50={pct(lats_v,0.5)}ms p90={pct(lats_v,0.9)}ms")
        print(f"tts    p50={pct(lats_t,0.5)}ms p90={pct(lats_t,0.9)}ms")
        print(f"total  p50={pct(lats_total,0.5)}ms p90={pct(lats_total,0.9)}ms")
    print(f"Wall time: {time.time()-t_start:.0f}s")

    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts:
            f.write(json.dumps({k:v for k,v in a.items() if k != 'pcm'}) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted:
            f.write(json.dumps({k:v for k,v in a.items() if k != 'pcm'}) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# AI commentary v3 — calibrated naming\n# {len(accepted)} accepted of {len(all_attempts)} calls\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")

    silence = bytearray(int(DURATION_S * SR_TTS * 2))
    dropped = 0
    for a in accepted:
        start_byte = int(a['scheduled_start_s'] * SR_TTS) * 2
        if start_byte >= len(silence): dropped += 1; continue
        usable = min(len(a['pcm']), len(silence) - start_byte)
        if usable > 0:
            silence[start_byte:start_byte+usable] = a['pcm'][:usable]
    with wave.open(str(OUT_AUDIO_WAV), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS); w.writeframes(bytes(silence))
    print(f"Wrote audio: {OUT_AUDIO_WAV}; dropped past 5min: {dropped}")

    subprocess.run(['ffmpeg','-y','-i',str(SOURCE_MP4),'-i',str(OUT_AUDIO_WAV),
                    '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','96k','-shortest',
                    str(OUT_MP4)], check=True, capture_output=True)
    subprocess.run(['ffmpeg','-y','-i',str(SOURCE_MP4),'-i',str(OUT_AUDIO_WAV),
        '-filter_complex',
        '[0:a]channelsplit=channel_layout=mono:channels=FC[ol];'
        '[1:a]aformat=channel_layouts=mono[gr];'
        '[ol][gr]join=inputs=2:channel_layout=stereo[a]',
        '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','128k',
        str(OUT_SBS)], check=True, capture_output=True)
    print(f"Wrote MP4s: {OUT_MP4}, {OUT_SBS}")


if __name__ == '__main__':
    main()

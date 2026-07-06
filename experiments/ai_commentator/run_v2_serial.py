#!/usr/bin/env python3
"""AI commentator v2: stricter naming, fallback vocabulary, gold-derived
style examples + per-player pre-game insights, simulated live-pacing.

Pacing model: walk bursts in time order. Only trigger a vision call if the
previous TTS would have *finished playing* by the time this burst's audio
would land. Mirrors how a single-mouth live booth self-paces — no parallel
generation that overflows the timeline.

Outputs ai_commentary_v2.*
"""
from __future__ import annotations
import base64, json, os, re, sys, time, subprocess, wave
import urllib.request, urllib.error
from pathlib import Path

# Manual env load
for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT_JSONL = BASE / 'commentary_v2.jsonl'
OUT_KEPT = BASE / 'commentary_v2_kept.txt'
OUT_AUDIO_WAV = BASE / 'ai_commentary_v2_track.wav'
OUT_MP4 = BASE / 'ai_commentary_v2.mp4'
OUT_SBS = BASE / 'ai_commentary_v2_sidebyside.mp4'
OUT_SCHED = BASE / 'commentary_v2_scheduled.jsonl'
SOURCE_MP4 = Path('/tmp/v2v_compare/slice_5min.mp4')

# worldcupvoice defaults
SAMPLE_INTERVAL_S = 0.55
CONTEXT_FRAMES = 4
MODEL = 'gpt-5.4-mini'
MAX_OUTPUT_TOKENS = 40
TEMPERATURE = 0.55
SR_TTS = 16000
DURATION_S = 300.0
DEDUP_JACCARD = 0.4   # tighter than v1's 0.7

EL_KEY = os.environ['ELEVENLABS_API_KEY']
EL_VOICE = os.environ.get('ELEVENLABS_VOICE_ID_EN_SPORTSCASTER') or os.environ['ELEVENLABS_VOICE_ID']
EL_MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_flash_v2_5')

# Per-player pre-game insights — derived ONLY from the actual broadcast STT,
# filtered to things knowable before kickoff (form, biography, role, manager
# preferences, departing-soon status, etc.). Anything tied to in-match
# events (goals, cards, this match's annulled goal) is excluded.
PLAYER_INSIGHTS = {
    'Burke': "In Scotland's World Cup squad picture; Eta has been giving him longer runs than Baumgart did.",
    'Sieb': "On a two-year loan from Bayern Munich, leaving at the end of the season — received a formal farewell before kickoff.",
    'Doekhi': "Dutch centre-back, born in Rotterdam; English clubs have been watching him.",
    'Weiper': "Tall striker, Germany U21 international; hasn't had things go his way much this campaign.",
    'Kohn': "Long-running broadcast joke about the 'Derrick' name coincidence with the classic German detective TV series.",
    'Zentner': "Strong at coming for crosses; one-on-ones are not his main strength.",
    'Veratschnig': "Very good recovery pace at the back.",
    'Juranovic': "Coming back from an injury-hit season.",
    'Lee': "South Korean midfielder (Jae-sung Lee).",
    'Posch': "Aerial threat — stands in the air well.",
}

MANAGER_INSIGHTS = {
    'Eta': "Marie-Louise Eta, first woman head coach in Bundesliga history — took over from Urs Fischer.",
    'Fischer': "Urs Fischer, former Union manager — title-winning architect whose contribution to the club will never be forgotten; went the wrong way at the end of his time in charge.",
}

# Style examples from gold — pure style, no spoilers, varied in shape
STYLE_EXAMPLES = [
    "Khedira leading that particular charge.",
    "At first sight, I'm with the referee.",
    "I think it's a race to the ball.",
    "Veratschnig venture forward. Here's Kohr.",
    "Mainz have been using the right-hand side quite a bit, but they've elected to attack from the left.",
    "Tall striker. Hasn't had things go his way too much in this campaign.",
    "Here at the Mewa Arena.",
    "Wide of the target.",
    "Yeah, home form is always vital, especially for smaller clubs.",
    "Trying to keep Juranovic in check.",
    "He has to be content with a corner.",
    "A little surprised he couldn't get his header on target.",
    "We're inside the final fifteen here.",
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
        'final_score_private': 'M05 1 - 3 UNI',  # kept private
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


# v2 prompt — stricter naming, fallback vocab, style examples
def build_visual_prompt(ctx_text, latest_time_s, previous_calls):
    previous = "\n".join(f"  - {c}" for c in previous_calls[-6:]) or "  - none yet"
    examples = "\n".join(f"  - \"{e}\"" for e in STYLE_EXAMPLES)
    return f"""You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers
who can see the same picture.

PROFILE: energetic American-English sportscaster style. Short, sharp, urgent
during attacks; reflective during lulls; restrained when the picture is
unclear.

VIDEO CONTEXT
Current video clock (relative to start of this slice): {latest_time_s:.1f}s.
You see a short burst of frames, oldest first, newest last. Comment on
whatever is happening in the NEWEST frame.

OUTPUT RULES
1. Length: usually 3-10 words, one sentence max. Sometimes a fragment.
2. Variety: rotate through these kinds of lines — do not always do the same:
   - call the visible action ("Mainz drive it down the right")
   - identify pass type or tempo ("a long diagonal", "neat short passes",
     "switch of play to the far side")
   - tactical observation ("Union dropping into a low block")
   - match-phase / time reference ("we're inside the final fifteen", "the
     closing stages here at the Mewa Arena")
   - atmosphere ("the crowd urging Mainz forward", "brief lull as both
     sides reset")
   - terse outcome ("wide of the target", "out for a corner")
   - colour aside about a player who is clearly the focus (only using the
     pre-game notes provided in the squad block — never invented)
3. Empty silence is fine. Return exactly "NO_CALL" when the newest frame
   is unreadable, no football action is visible, or it's clearly a static
   timeout / replay / crowd-only shot. Use NO_CALL more often than you
   think — silence is normal in football commentary.

STRICT NAMING RULE
- Only name a player if their shirt number is *legible* in the newest frame
  AND that number matches the squad for the team whose kit they wear.
- If you cannot read the number, DO NOT GUESS. Default to a generic role
  description: "the Mainz striker", "Union's right winger", "Mainz's keeper",
  "the number 7 in red", "a Union defender".
- Players are substituted during matches. Even if you named "Becker" in a
  previous call, the player you see now might be his replacement — do NOT
  carry forward a name from earlier frames. Re-check the number each call.
- A correct generic description is much better than a wrong name.

NO INVENTION
- No goals, cards, scores, substitutions, or events that are not visibly
  in the newest frame. Do not announce things that haven't happened.
- The known final result is private metadata — do not reference it.

STYLE EXAMPLES from a real broadcast booth (illustrative, do NOT transcribe):
{examples}

CONTEXT
{ctx_text}

RECENT CALLS you have just made — do not repeat the same observation:
{previous}

Now produce your next call (3-10 words, one sentence, or NO_CALL):
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
    print(f"Context text size: {len(ctx_text)} chars")
    print(f"Players with pre-game insight: {sum(1 for p in ctx['roster'] if p['insight'])}/{len(ctx['roster'])}")

    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

    # Walk bursts in time order, simulating live-booth pacing
    bursts = []
    for i in range(CONTEXT_FRAMES - 1, len(frame_paths)):
        bursts.append((i, (i + 1) * SAMPLE_INTERVAL_S, frame_paths[i - CONTEXT_FRAMES + 1 : i + 1]))
    print(f"Bursts available: {len(bursts)}")

    accepted = []     # list of dicts with full schedule info + pcm
    skipped_bursts = 0
    no_call_count = 0
    repetitive_count = 0
    error_count = 0
    booth_busy_until = 0.0   # source video time at which previous clip would finish
    LIVE_GAP_S = 0.20        # small breathing room between clips

    all_attempts = []
    last_print = time.time()
    t_start = time.time()

    for burst_idx, (i, latest_time_s, burst) in enumerate(bursts):
        # Live-pace gate: skip if booth is still busy from a previous clip
        if latest_time_s < booth_busy_until + LIVE_GAP_S:
            skipped_bursts += 1
            continue
        prev_texts = [a['text'] for a in accepted[-6:]]
        text, vision_ms, err = call_vision(client, burst, latest_time_s, prev_texts, ctx_text)
        attempt = {
            'burst_index': i,
            'video_time_s': round(latest_time_s, 2),
            'vision_latency_ms': vision_ms,
            'text': text,
            'accepted': False,
            'reason': None,
            'error': err,
        }
        if err:
            error_count += 1
            attempt['reason'] = 'error'
        elif text is None or text == '':
            attempt['reason'] = 'empty'
        elif is_no_call(text):
            no_call_count += 1
            attempt['reason'] = 'no_call'
        elif is_repetitive(text, [a['text'] for a in accepted], DEDUP_JACCARD):
            repetitive_count += 1
            attempt['reason'] = 'repetitive'
        else:
            # Accepted — TTS now, schedule, update booth_busy_until
            try:
                pcm, tts_ms = tts_one(text)
            except urllib.error.HTTPError as e:
                attempt['reason'] = f'tts_error_{e.code}'
                all_attempts.append(attempt)
                continue
            duration_s = len(pcm) / 2 / SR_TTS
            realistic_lag_s = (vision_ms + tts_ms) / 1000.0
            scheduled_start_s = latest_time_s + realistic_lag_s
            scheduled_end_s = scheduled_start_s + duration_s
            attempt.update({
                'accepted': True,
                'tts_ms': tts_ms,
                'duration_s': round(duration_s, 3),
                'realistic_lag_s': round(realistic_lag_s, 3),
                'scheduled_start_s': round(scheduled_start_s, 3),
                'scheduled_end_s': round(scheduled_end_s, 3),
            })
            accepted.append({**attempt, 'pcm': pcm})
            booth_busy_until = scheduled_end_s

        all_attempts.append(attempt)

        if time.time() - last_print > 8:
            elapsed = time.time() - t_start
            print(f"  burst {burst_idx}/{len(bursts)}  video_t={latest_time_s:.1f}s  "
                  f"accepted={len(accepted)}  skipped={skipped_bursts}  "
                  f"no_call={no_call_count}  rep={repetitive_count}  "
                  f"booth_busy_until={booth_busy_until:.1f}s  elapsed={elapsed:.0f}s")
            last_print = time.time()

    print(f"\n=== Pipeline summary ===")
    print(f"Total bursts: {len(bursts)}")
    print(f"Vision calls made: {len(all_attempts)}")
    print(f"Skipped (booth busy): {skipped_bursts}")
    print(f"NO_CALL: {no_call_count}  Repetitive: {repetitive_count}  Errors: {error_count}")
    print(f"Accepted + TTS'd + scheduled: {len(accepted)}")
    if accepted:
        lats_v = sorted(a['vision_latency_ms'] for a in accepted)
        lats_t = sorted(a['tts_ms'] for a in accepted)
        lats_total = sorted(int(a['realistic_lag_s']*1000) for a in accepted)
        def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
        print(f"vision latency p50={pct(lats_v,0.5)}ms p90={pct(lats_v,0.9)}ms")
        print(f"tts    latency p50={pct(lats_t,0.5)}ms p90={pct(lats_t,0.9)}ms")
        print(f"total  latency p50={pct(lats_total,0.5)}ms p90={pct(lats_total,0.9)}ms")
    print(f"Wall time: {time.time()-t_start:.0f}s")

    # Write all attempts log
    with open(OUT_JSONL, 'w') as f:
        for a in all_attempts:
            f.write(json.dumps({k: v for k, v in a.items() if k != 'pcm'}) + '\n')
    with open(OUT_SCHED, 'w') as f:
        for a in accepted:
            f.write(json.dumps({k: v for k, v in a.items() if k != 'pcm'}) + '\n')
    with open(OUT_KEPT, 'w') as f:
        f.write(f"# AI commentary v2 — strict naming + fallback vocab + gold-derived examples\n")
        f.write(f"# {len(accepted)} accepted of {len(all_attempts)} vision calls made\n\n")
        for a in accepted:
            f.write(f"[{a['video_time_s']:7.2f}s] {a['text']}\n")

    # Build audio track
    silence = bytearray(int(DURATION_S * SR_TTS * 2))
    dropped_overrun = 0
    for a in accepted:
        start_byte = int(a['scheduled_start_s'] * SR_TTS) * 2
        if start_byte >= len(silence):
            dropped_overrun += 1
            continue
        usable = min(len(a['pcm']), len(silence) - start_byte)
        if usable > 0:
            silence[start_byte:start_byte + usable] = a['pcm'][:usable]
    with wave.open(str(OUT_AUDIO_WAV), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS)
        w.writeframes(bytes(silence))
    print(f"Wrote {OUT_AUDIO_WAV}; dropped past 5min: {dropped_overrun}")

    # Mux MP4
    subprocess.run(['ffmpeg','-y','-i',str(SOURCE_MP4),'-i',str(OUT_AUDIO_WAV),
                    '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','96k',
                    '-shortest', str(OUT_MP4)], check=True, capture_output=True)
    print(f"Wrote {OUT_MP4}")
    # Side-by-side
    subprocess.run(['ffmpeg','-y','-i',str(SOURCE_MP4),'-i',str(OUT_AUDIO_WAV),
        '-filter_complex',
        '[0:a]channelsplit=channel_layout=mono:channels=FC[ol];'
        '[1:a]aformat=channel_layouts=mono[gr];'
        '[ol][gr]join=inputs=2:channel_layout=stereo[a]',
        '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','128k',
        str(OUT_SBS)], check=True, capture_output=True)
    print(f"Wrote {OUT_SBS}")


if __name__ == '__main__':
    main()

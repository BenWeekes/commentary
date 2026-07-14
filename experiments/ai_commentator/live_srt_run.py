#!/usr/bin/env python3
"""Live SRT-in → gpt-5.5 playerist → EN + FR audio-out — real-time proof.

Pipeline:
  1. ffmpeg -re pushes clips/m05_uni_eval_25min/source.mp4 to srt://localhost:PORT
     at wall-clock speed (this simulates the live match feed).
  2. This script's frame reader pulls JPEG frames every 0.55 s from the SRT
     stream via ffmpeg image2pipe.
  3. Booth-busy gate: whenever the booth (either EN or FR audio track) is free,
     we call gpt-5.5 vision on the last 4 frames + playerist prompt.
  4. Each accepted line is translated to FR (gpt-5.4-mini, cheap) then sent
     to two ElevenLabs eleven_v3 TTS calls in parallel (EN voice, FR voice).
  5. Per-burst wall time is logged with (arrived_at, published_at, lag).
  6. When the source clip ends, we mux the source video against the two
     recorded audio tracks and publish v13_live_en.mp4 + v13_live_fr.mp4.

Wall time to complete = ~5:00 clip + a few seconds of tail-flush.
Real-time verification: no burst can be scheduled EARLIER than its
frame's wall-clock arrival time — proves the pipeline keeps up.
"""
from __future__ import annotations
import base64, json, os, re, sys, time, subprocess, threading, wave
import urllib.request, urllib.error
import concurrent.futures
from collections import deque
from pathlib import Path

for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from openai import OpenAI
sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_v4 import summarise_alias_usage
from run_v5 import (
    is_repetitive_trigram, detect_sub, format_sub_history, format_pitch_state,
    cheap_tag_guess, build_match_context, is_no_call,
)
from rich_context import build_rich_context_text
from run_gpt55_variant import VARIANT_PROMPTS
from repetition_helpers import (
    summarise_verb_usage, summarise_player_usage,
    summarise_referee_usage, is_repeated_waiting,
)

# ---- pipeline registry ----
# Which model + prompt combo to run. Selected via --pipeline flag.
PIPELINE_CONFIGS = {
    'high': {
        'model': 'gpt-5.5',
        'prompt_style': 'playerist',
        'max_output_tokens': 400,
        'srt_port': 10082,
        'frames_dir': '/tmp/live_frames_high',
        'out_prefix': 'v13_live',
        'sample_interval_s': 0.55,
        'burst_frames': 4,
    },
    # denser + more frames experiment (regressed hallu, keeping for reference)
    'high_dense': {
        'model': 'gpt-5.5',
        'prompt_style': 'playerist',
        'max_output_tokens': 400,
        'srt_port': 10084,
        'frames_dir': '/tmp/live_frames_high_dense',
        'out_prefix': 'v15_live',
        'sample_interval_s': 0.3,
        'burst_frames': 7,
    },
    # Ablation A: current improvements minus replay detection
    'high_noreplay': {
        'model': 'gpt-5.5',
        'prompt_style': 'playerist',
        'max_output_tokens': 400,
        'srt_port': 10085,
        'frames_dir': '/tmp/live_frames_high_noreplay',
        'out_prefix': 'v16_live',
        'sample_interval_s': 0.55,
        'burst_frames': 4,
    },
    # Ablation B: current improvements + "generic-first" hoisted
    'high_genericfirst': {
        'model': 'gpt-5.5',
        'prompt_style': 'playerist',
        'max_output_tokens': 400,
        'srt_port': 10086,
        'frames_dir': '/tmp/live_frames_high_generic',
        'out_prefix': 'v17_live',
        'sample_interval_s': 0.55,
        'burst_frames': 4,
    },
    # SHORT-SLICE SWEEP variants (all target the 60s sub-heavy problem zone)
    # Runner uses AI_COMMENTATOR_SOURCE=/tmp/v2v_compare/slice_subs_60s.mp4
    'sweep_baseline': {
        'model': 'gpt-5.5', 'prompt_style': 'playerist',
        'max_output_tokens': 400, 'srt_port': 10091,
        'frames_dir': '/tmp/live_frames_sweep_baseline',
        'out_prefix': 'sweep_baseline',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    'sweep_wider': {  # 4 frames at 1.0s = 4s window
        'model': 'gpt-5.5', 'prompt_style': 'playerist',
        'max_output_tokens': 400, 'srt_port': 10092,
        'frames_dir': '/tmp/live_frames_sweep_wider',
        'out_prefix': 'sweep_wider',
        'sample_interval_s': 1.0, 'burst_frames': 4,
    },
    'sweep_more_wide': {  # 6 frames at 1.0s = 6s window
        'model': 'gpt-5.5', 'prompt_style': 'playerist',
        'max_output_tokens': 400, 'srt_port': 10093,
        'frames_dir': '/tmp/live_frames_sweep_more_wide',
        'out_prefix': 'sweep_more_wide',
        'sample_interval_s': 1.0, 'burst_frames': 6,
    },
    'sweep_anchor': {  # 4 frames at 0.55s + 2 anchor frames from further back (via env)
        'model': 'gpt-5.5', 'prompt_style': 'playerist',
        'max_output_tokens': 400, 'srt_port': 10094,
        'frames_dir': '/tmp/live_frames_sweep_anchor',
        'out_prefix': 'sweep_anchor',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    # v18 — production candidate combining ablation + sweep wins.
    # Soft "generic-first" framing at top of prompt (no strict ban list),
    # plus anchor frames from -5s and -10s for sub/booking grounding.
    # Set AI_COMMENTATOR_GENERIC_FIRST=1 + AI_COMMENTATOR_ANCHOR_OFFSETS_S=5.0,10.0
    'high_v18': {
        'model': 'gpt-5.5', 'prompt_style': 'playerist',
        'max_output_tokens': 400, 'srt_port': 10087,
        'frames_dir': '/tmp/live_frames_high_v18',
        'out_prefix': 'v18_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    # Low-tier v18 companion — same tweaks on gpt-5.4-mini
    'low_v18': {
        'model': 'gpt-5.4-mini', 'prompt_style': 'v5',
        'max_output_tokens': 80, 'srt_port': 10088,
        'frames_dir': '/tmp/live_frames_low_v18',
        'out_prefix': 'v18_low_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    # v19 — two-stage: SAFE-draft vision + text polisher (produces EN + FR).
    # Vision must be aggressively safe (may repeat); polisher adds variety.
    'high_v19': {
        'model': 'gpt-5.5', 'prompt_style': 'safe_draft',
        'max_output_tokens': 200, 'srt_port': 10089,
        'frames_dir': '/tmp/live_frames_high_v19',
        'out_prefix': 'v19_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    'low_v19': {
        'model': 'gpt-5.4-mini', 'prompt_style': 'safe_draft',
        'max_output_tokens': 100, 'srt_port': 10090,
        'frames_dir': '/tmp/live_frames_low_v19',
        'out_prefix': 'v19_low_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    # v20 = v19 but polisher is gpt-5.5 (better prose flow, +latency, ×5 cost/line)
    'high_v20': {
        'model': 'gpt-5.5', 'prompt_style': 'safe_draft',
        'polisher_model': 'gpt-5.5',
        'max_output_tokens': 200, 'srt_port': 10091,
        'frames_dir': '/tmp/live_frames_high_v20',
        'out_prefix': 'v20_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    'low_v20': {
        'model': 'gpt-5.4-mini', 'prompt_style': 'safe_draft',
        'polisher_model': 'gpt-5.5',
        'max_output_tokens': 100, 'srt_port': 10092,
        'frames_dir': '/tmp/live_frames_low_v20',
        'out_prefix': 'v20_low_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
    },
    # v20_par = high_v20 but vision+polish run in a worker pool so a slow
    # (CPU-contention-starved) call can't freeze the main loop. See main_parallel().
    'high_v20_par': {
        'model': 'gpt-5.5', 'prompt_style': 'safe_draft',
        'polisher_model': 'gpt-5.5',
        'max_output_tokens': 200, 'srt_port': 10093,
        'frames_dir': '/tmp/live_frames_high_v20_par',
        'out_prefix': 'v20_par_live',
        'sample_interval_s': 0.55, 'burst_frames': 4,
        'parallel': True, 'vision_concurrency': 4,
    },
    'low': {
        'model': 'gpt-5.4-mini',
        'prompt_style': 'v5',
        'max_output_tokens': 80,
        'srt_port': 10083,
        'frames_dir': '/tmp/live_frames_low',
        'out_prefix': 'v14_live',
        'sample_interval_s': 0.55,
        'burst_frames': 4,
    },
}


# Selected at bottom of file via CLI
PIPELINE = None  # set by main()
MODEL = None
POLISHER_MODEL = 'gpt-5.4-mini'  # default; overridable per pipeline config
MAX_OUTPUT_TOKENS = None
SRT_PORT = None
SRT_URL_SEND = None
SRT_URL_RECV = None
LIVE_FRAMES_DIR = None
OUT_PREFIX = None
PROMPT_STYLE = None
VISION_CONCURRENCY = 4  # set by CLI for parallel pipelines

SOURCE_MP4 = Path(os.environ.get('AI_COMMENTATOR_SOURCE', '/tmp/v2v_compare/slice_5min.mp4'))
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')

SAMPLE_INTERVAL_S = 0.55  # set by CLI
CONTEXT_FRAMES = 4        # set by CLI
NATURAL_LAG_S = 0.3

EL_KEY = os.environ['ELEVENLABS_API_KEY']
EN_VOICE = 'gU0LNdkMOQCOrPrwtbee'
FR_VOICE = 'LcKoSBj8CeBInl4bQHtq'
EL_MODEL = 'eleven_v3'
SR_TTS = 16000
DURATION_S = 300.0

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


def encode_jpeg(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


# ---- CPU-side motion analysis ----
# Compute mean absolute pixel difference between newest and previous burst frames.
# Lightweight (numpy + PIL), ~5-15 ms. Used to give the vision LLM a motion hint
# in the prompt without asking it to compute across frames itself.
try:
    import numpy as _np
    from PIL import Image as _Image
    _MOTION_OK = True
except ImportError:
    _MOTION_OK = False

_MOTION_HISTORY = []   # rolling list of recent motion values
_MOTION_DS_W = 128
_MOTION_DS_H = 72


def _load_gray_ds(path):
    with _Image.open(path) as img:
        img = img.convert('L').resize((_MOTION_DS_W, _MOTION_DS_H), _Image.BILINEAR)
        return _np.asarray(img, dtype=_np.int16)


def compute_motion_hint(burst_paths):
    """Return (motion_value, motion_level_str, hint_text) or (None, None, '').

    motion_level_str ∈ {'high','normal','low','replay'} vs a rolling baseline.
    hint_text is a one-liner to inject into the prompt.
    """
    if not _MOTION_OK or len(burst_paths) < 2:
        return None, None, ''
    try:
        # Compare the newest frame against the frame ~1.1s before (index -3 in burst)
        a = _load_gray_ds(burst_paths[-1])
        b = _load_gray_ds(burst_paths[max(0, len(burst_paths)-3)])
        m = float(_np.abs(a - b).mean())
    except Exception:
        return None, None, ''
    _MOTION_HISTORY.append(m)
    if len(_MOTION_HISTORY) > 30:
        _MOTION_HISTORY.pop(0)
    if len(_MOTION_HISTORY) < 5:
        return m, 'normal', f"  MOTION HINT: baseline still forming (current={m:.1f})."
    baseline = float(_np.median(_MOTION_HISTORY[-25:]))
    ratio = m / max(baseline, 1.0)
    if ratio >= 1.5:
        level = 'high'
    elif ratio >= 0.6:
        level = 'normal'
    elif ratio >= 0.2:
        level = 'low'
    else:
        level = 'replay'
    hint = (
        f"  MOTION HINT (from pixel analysis, not from you): "
        f"current burst motion is {int(ratio*100)}% of recent baseline "
        f"(≈{m:.1f} vs baseline {baseline:.1f}). Level: {level}. "
    )
    if level == 'replay':
        hint += "This is likely a slow-motion REPLAY segment — expect a canned replay line."
    elif level == 'low':
        hint += "Frame is quiet — describe posture / positions only, avoid claiming actions."
    return m, level, hint


# =====================================================================
# v19 — two-stage: SAFE-draft vision + text polisher
# =====================================================================

SAFE_DRAFT_PROMPT = """You are the FIRST STAGE of a two-stage football
commentary pipeline. Your ONLY job is to describe what is VISIBLY TRUE in
the newest frame — no more, no less.

STRICT OUTPUT RULES (violating these breaks the pipeline downstream):

  Allowed content:
    - Player identity (from shirt number, kit colour, position, roster)
    - Ball location (zone: attacking third, penalty area, midfield, own half,
      touchline, edge of box, six-yard box, corner)
    - Static ball state (over the ball, at the ball, near the ball,
      in possession, on the touchline, in the goalmouth)
    - PASS IN PROGRESS — NAME THE RECEIVER. If the ball is clearly travelling
      from one player to an identifiable team-mate, name the receiver with a
      PLAIN pass phrase: "to <Surname>", "played to <Surname>", "square to
      <Surname>", "back to <Surname>". Do this AS OFTEN AS POSSIBLE whenever a
      pass is visible and the receiver is identifiable (shirt number / position
      / roster). Plain pass phrasing only — NOT the banned flashy verbs below.
    - Referee position ("the referee near midfield")
    - Substitution board visible → name both players from roster
    - Team tactical phase ("home side in possession", "visitors defending deep")

  BANNED — never use these unless the action is UNAMBIGUOUSLY MID-EXECUTION
  in the frame (a ball being kicked, a save being made, a card being shown):
    save, saves, denies, punches, claws, claims, catches, gathers, sweeps
    tackle, tackles, intercepts, blocks, clears, wins the ball, nicks
    shoot, shoots, strikes, fires, blasts, buries, volleys, heads, flicks
    cross, crosses, whips, floats, threads, chips, curls, delivers, sends
    drives, sprints, races, tears, bursts, breaks, weaves, dribbles
    scores, goal, celebrates, applauds, wheels away, mobs, hugs
    starts his run-up, approaches, sizes up, weighs up, prepares
    dejected, disappointed, angry, animated, gesturing, protesting

  BANNED adjectives / adverbs (imply subjective judgment):
    brave/bravely, unhurried, patient/patiently, calm/calmly, urgent/urgently,
    dangerous, threatening, incisive, clever, brilliant, dominant, comfortable

  Length: 3-10 words. One clause. Present tense.

  Fine examples:
    "Klaus at his goalmouth."
    "Amiri over the ball at the edge of the box."
    "Amiri to Caci on the left."
    "Played square to Kohr."
    "Back to Zentner."
    "Mainz shirts in the attacking third."
    "Referee near the incident."
    "The ball at the near touchline."
    "Sub board up: Weiper on, Tietz off."

  Bad examples (would break the pipeline):
    "Amiri whips it in."           (banned event verb)
    "Klaus bravely comes out."     (banned adjective)
    "Mainz threaten with pace."    (banned adjective + speculative)

  If nothing is visibly worth saying (routine possession, static, replay,
  crowd shot): return NO_CALL.

Output only the safe draft as a bare phrase (no prefix, no quotes)."""


POLISHER_SYSTEM = """You are a live BBC-style football commentator polishing a
bare visual description into natural broadcast prose. You receive a SAFE DRAFT
from the vision model (which literally describes what is visible in the frame)
and the last few accepted commentary lines. Your job is to rewrite the SAFE
DRAFT into ONE natural broadcaster sentence in ENGLISH AND FRENCH, with the
rhythm and variety of a real live-TV commentator.

Think of yourself as translating "camera facts" into "broadcaster speech".

HARD RULES (violating these = pipeline failure):
  1. Do NOT introduce new PLAYERS, TEAMS, or EVENTS (goals, saves, cards,
     substitutions, tackles, shots, crosses) not in the SAFE DRAFT.
  2. Same SUBJECT as the draft. If draft is about Klaus you cannot rewrite
     it to be about Zentner. If about "the referee" you cannot make it about
     a specific named player.
  3. Same tense (present) and same fragment/sentence style.
  4. If the safe draft literally starts with "REPLAY:", it has ALREADY been
     handled upstream — you will never receive one. Ignore any REPLAY prefix
     if you somehow see it and DO NOT include "REPLAY:" in your JSON output.
  5. Length: 5-14 words. Natural spoken cadence.

STYLING FREEDOM you DO have (use it):
  - Reorder the parts of the description.
  - Add natural connective phrasing ("with the ball at his feet",
    "watching the play develop", "still in position", "unhurried on it").
  - Add generic tempo / phase words (patient, quiet, level, steady, holding).
    These are OK because they describe the OBSERVED pace, not an event.
  - Add generic location colour ("deep in his own half", "just outside the box",
    "wide on the touchline") if the safe draft implied it.
  - Match variety across recent lines — don't reuse the same verb/subject/
    opener twice in three lines.
  - PASSES: if the draft names a pass receiver ("to <Surname>", "played to
    <Surname>"), KEEP the receiver's name and lead with the pass — "…finds
    <Surname>", "…and it's to <Surname>", "<Surname> receives" — that naming
    is the most natural football commentary. Never drop the named receiver.

DANGER LIST (imply unseen events, avoid these):
  - dangerous, threatening, brilliant, incisive, clever, decisive
  - about to shoot / cross / tackle / save
  - "makes way for", "coming on for" — only if the draft names the sub
  - Any specific-action verb (whips, floats, drives, curls) unless the draft
    literally has it

FRENCH-SPECIFIC guidance (idiomatic football commentary):
  - gap / opening → faille, ouverture (NOT brèche)
  - set piece / dead ball → coup de pied arrêté, ballon arrêté
  - wall → mur défensif
  - cross → centre
  - final third → dernier tiers, trente derniers mètres
  - hosts → les locaux (NOT les hôtes)
  - visitors → les visiteurs
  - the referee → l'arbitre, M. Exner
  - goalkeeper → gardien
  - Preserve all proper nouns (player, team, place, manager names) EXACTLY.
  - Fragment structure: match the source. Present tense.

Return ONLY strict JSON, no code fence, no explanation:
{"en": "polished English", "fr": "natural French"}"""


def polish_line(safe_draft, previous_lines):
    """Second-stage text-only call — polish safe draft into broadcast prose in
    BOTH English and French. Returns (en_text, fr_text, latency_ms).

    Model selected via global POLISHER_MODEL (set at CLI time). gpt-5.5 gives
    better prose flow; gpt-5.4-mini is faster and cheaper."""
    recent = "\n".join(f"  - {c}" for c in previous_lines[-12:]) or "  - none"
    user = f"""SAFE DRAFT: {safe_draft!r}

RECENT ACCEPTED LINES in English (avoid repeating their exact phrasings and verbs):
{recent}

Produce JSON with polished English and natural French of the safe draft."""
    t0 = time.monotonic()
    kwargs = dict(
        model=POLISHER_MODEL,
        max_completion_tokens=300,
        messages=[
            {"role": "system", "content": POLISHER_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    # gpt-5.5 uses reasoning_effort, not temperature; mini uses default temp.
    if POLISHER_MODEL.startswith('gpt-5.5'):
        kwargs['reasoning_effort'] = 'low'
    try:
        resp = client.chat.completions.create(**kwargs)
        raw = (resp.choices[0].message.content or '').strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return safe_draft, '', int((time.monotonic()-t0)*1000)
        en = (obj.get('en') or safe_draft).strip().strip('"')
        fr = (obj.get('fr') or '').strip().strip('"')
        return en, fr, int((time.monotonic()-t0)*1000)
    except Exception as e:
        return safe_draft, '', int((time.monotonic()-t0)*1000)


def vision_call(burst_paths, prompt):
    """Model-agnostic vision call. Uses MODEL + reasoning=low if gpt-5.5."""
    content = [{"type": "input_text", "text": prompt}]
    for p in burst_paths:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encode_jpeg(p)}"})
    kwargs = dict(
        model=MODEL,
        input=[{"role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    if MODEL.startswith('gpt-5.5'):
        kwargs['reasoning'] = {"effort": "low"}
    else:
        kwargs['temperature'] = 0.55
    t0 = time.monotonic()
    try:
        resp = client.responses.create(**kwargs)
        return (resp.output_text or '').strip(), int((time.monotonic()-t0)*1000), None
    except Exception as e:
        return None, int((time.monotonic()-t0)*1000), f"ERR: {e}"


# ---- Prompt bodies ----------------------------------------------------

V5_PROMPT_BODY = """You are a live English football play-by-play commentator on a Bundesliga
broadcast. You are NOT an image captioner — you are speaking live to viewers.

PROFILE: experienced English-language sportscaster. Short, sharp, urgent during
attacks; reflective during lulls; restrained when the picture is unclear.

VIDEO CONTEXT
You see a short burst of frames, oldest first, newest last. The FIRST frame is
carry-over from the previous burst — use it for continuity. Comment on the
NEWEST frame.

OUTPUT — KEEP IT SHORT, KEEP IT SPARSE
- 3-12 words, one sentence or a fragment.
- NO_CALL is right about 40-50% of the time. A skilled commentator goes QUIET
  during routine possession — speech is reserved for moments of consequence.
- If you'd just be paraphrasing your previous call with different wording,
  return NO_CALL.

NAMING — LEAN INTO IT
- NAME PLAYERS WHENEVER REASONABLE. Strong identification is the heart of good
  commentary. Use the shirt number visible on the back when you can.
- Goalkeepers: Klaus for Union, Zentner for Mainz.
- DO NOT invent names not on either roster.

SUBSTITUTIONS
- 4th-official electronic board: RED top = off, GREEN bottom = on.
- DO NOT re-announce a sub already listed in "SUBS ALREADY ANNOUNCED".

SCORELINE RULE
- DO NOT state the scoreline unless it just changed this burst.

SET-PIECE TEAM ATTRIBUTION
- Only name a team when clearly seen picking up the ball. Otherwise describe
  the set piece without naming a team.

GENERIC OVER INCORRECT.
"""


def build_prompt(rich_ctx, latest_time_s, previous, alias_usage, sub_hist, pitch_state,
                 referee_usage=None, motion_hint=None):
    previous_lines = "\n".join(f"  - {c}" for c in previous[-12:]) or "  - none"

    # Ablation flags (env vars) for controlled experiments
    # CPU-side replay detection now handles this — prompt-side REPLAY: rule is
    # OFF by default (LLM-based detection was unreliable, added false positives).
    # Set AI_COMMENTATOR_LLM_REPLAY=1 to re-enable the prompt-based version.
    no_replay = os.environ.get('AI_COMMENTATOR_LLM_REPLAY') != '1'
    generic_first = os.environ.get('AI_COMMENTATOR_GENERIC_FIRST') == '1'

    generic_first_header = ""
    if generic_first:
        # SOFT variant of the "generic first" framing — top-of-prompt reminder,
        # no banned-verb list. v17 with the ban list dropped hallu to 9% but
        # over-silenced the model (32 lines, 73% coverage). v18 softens this
        # so we get the framing effect without the coverage collapse.
        generic_first_header = """
================================================================
TOP-PRIORITY RULE — GENERIC OVER INCORRECT
================================================================
Before EVERY line, ask ONE question: "Can I LITERALLY SEE this happening
in the newest frame right now?" If not, downgrade the claim.

Specifically for event verbs (save, tackle, shot, cross, drives, buries,
scores, celebrates, gathers, floats, whips, etc.): only use them if the
action is CLEARLY MID-EXECUTION in the newest frame. A player near the
ball is NOT the same as a player shooting or driving. A goalkeeper
watching the ball is NOT the same as a goalkeeper saving.

When uncertain, describe what IS visible: player identity + ball location
+ tactical phase. That is always safe.

Coverage matters — don't return NO_CALL just because you want to be safe.
Speak, but with generic wording when the specific event isn't verifiable.
================================================================
"""
    # v18 hard variant kept for A/B — env var AI_COMMENTATOR_GENERIC_STRICT=1
    if os.environ.get('AI_COMMENTATOR_GENERIC_STRICT') == '1':
        generic_first_header += """
BANNED unless mid-execution visible: save/denies/punches/claws/claims/tackles/
intercepts/blocks/clears/gathers/shoots/strikes/fires/blasts/buries/volleys/
heads/flicks/whips/floats/threads/chips/curls/delivers/crosses/drives/runs/
sprints/scores/celebrates/starts-a-run-up/begins-his-approach/sizes-up.
"""

    if PROMPT_STYLE == 'safe_draft':
        # v19 stage 1: bare visible-facts description. Polisher (2nd LLM) turns
        # this into broadcast prose.
        body = SAFE_DRAFT_PROMPT
    elif PROMPT_STYLE == 'playerist':
        body = f"""{generic_first_header}You are a live English football commentator on a Bundesliga broadcast.

You see a burst of frames (oldest first, last one is NEWEST). Comment on
the NEWEST frame. The first frame is carry-over from the previous burst.

{VARIANT_PROMPTS['playerist']}
"""
    else:  # 'v5'
        body = generic_first_header + V5_PROMPT_BODY

    referee_block = f"\n{referee_usage}" if referee_usage else ""
    # motion_hint is intentionally NOT injected into the prompt anymore —
    # the CPU-side motion detector handles slow-mo by short-circuiting to a
    # canned replay line before the vision call. See main-loop accept path.
    _ = motion_hint

    replay_block = "" if no_replay else """
REPLAY DETECTION — before your call:
- Broadcast replays typically show slow-motion, distinct camera angle, a
  "SLOWMO" or replay graphic, or clearly repeat a moment we just discussed.
- If the newest frame looks like a REPLAY (slow-motion feel, replay graphic
  visible, or clearly re-showing an earlier moment), PREFIX your output with
  the literal token "REPLAY: yes\\n" on its own line, THEN write nothing else
  (the system will insert a canned replay line automatically).
- If not a replay, PREFIX with "REPLAY: no\\n" then your normal call on the
  next line.
- Every output must start with REPLAY: yes or REPLAY: no.
"""

    return f"""{body}
{replay_block}
WAITING LINES — a NEW rule:
- If the previous line already noted that a player is waiting / standing over
  the ball / poised to restart, and NOTHING has visibly changed in the newest
  frame, return NO_CALL. Do not repeat "X is waiting" in consecutive lines.

REFEREE NAMING:
- The match referee's name is Florian Exner. Do NOT keep saying just "Exner"
  every time — that reads as unnatural. Alternate between "Exner", "Mr Exner",
  "the referee", and "the official". If you referred to the ref recently, use
  a different form this turn.
{referee_block}

VIDEO CLOCK: {latest_time_s:.1f}s

SUBS ALREADY ANNOUNCED (do not re-announce):
{sub_hist}

PITCH STATE:
{pitch_state or "(starting XIs unchanged)"}

RECENT ACCEPTED LINES:
{previous_lines}

TEAM ALIAS USAGE IN LAST 3 LINES:
{alias_usage}

RICH PRE-GAME CONTEXT
{rich_ctx}

Produce your next call (or NO_CALL):
"""


TRANSLATE_SYSTEM = """Translate the given English football commentary line
into natural, idiomatic French SPORTS-commentary voice — the way it would
sound on a live Ligue 1 or Champions League TV broadcast.

Same length or shorter. Preserve player / team / place / manager names EXACTLY.
Return ONLY the French translation, no quotes, no explanation.

FOOTBALL-NATIVE FRENCH — use these preferred renderings:

  gap / opening in the defence → "faille", "ouverture", "brèche défensive"
    (avoid bare "brèche" — unnatural in football)
  set piece / dead ball → "coup de pied arrêté", "ballon arrêté", "coup franc"
  wall (defensive) → "mur défensif" (not "muraille")
  cross → "centre" (not "traversée")
  through-ball → "passe en profondeur"
  final third → "dernier tiers", "trente derniers mètres"
  breakaway / counter → "contre-attaque", "contre"
  back-heel → "talonnade"
  header → "coup de tête" / "tête"
  save → "arrêt", "parade"
  clearance → "dégagement"
  tackles / tackle → "tacle", "tacler"
  offside → "hors-jeu"
  hosts / home side → "les locaux", "l'équipe à domicile" (avoid "les hôtes")
  visitors / away side → "les visiteurs", "l'équipe visiteuse"
  Union / Iron Ones (Union Berlin's nickname) → "l'Union" / "les Eisernen"
  the 05ers (Mainz's nickname) → "les Mayençais" or "Mayence 05"
  substitution → "changement", "remplacement"
  the referee → "l'arbitre", "M. Exner" (do not literalize "the whistle")
  Exner → "Exner" (proper noun, unchanged)
  goalkeeper → "gardien" (not "gardien de but" every time)

FRAGMENT STRUCTURE:
  Match the source. If English is a fragment, keep French as a fragment.
  "Klaus waits over the restart." → "Klaus attend la reprise." NOT a full sentence.

TENSE:
  Live commentary is present tense in French. Use it.
"""


def translate_fr(en):
    resp = client.chat.completions.create(
        model='gpt-5.4-mini',
        max_completion_tokens=200,
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": en},
        ],
    )
    return (resp.choices[0].message.content or '').strip().strip('"')


def tts(text, voice):
    body = json.dumps({"text": text, "model_id": EL_MODEL,
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        data=body, headers={'xi-api-key': EL_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


TAG_SYSTEM = """Tag a football commentary line for expressive TTS.
Pick ONE tag from: [calm] [flatly] [excited] [nervous] [frustrated] [sorrowful]
[resigned tone] [whispers] [deadpan] [cheerfully]. Default [calm]. Output only bracketed tag."""


def pick_tag(text):
    try:
        resp = client.chat.completions.create(
            model='gpt-5.4-mini', max_completion_tokens=50,
            messages=[{"role": "system", "content": TAG_SYSTEM},
                      {"role": "user", "content": text}],
        )
        raw = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\[[a-z ]+\]', raw)
        return m.group(0) if m else '[calm]'
    except Exception:
        return '[calm]'


# ---- SRT sender + receiver ----

def start_srt_sender():
    """ffmpeg -re pushes source MP4 to SRT listener at wall-clock speed."""
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-re', '-i', str(SOURCE_MP4),
        '-c:v', 'copy', '-c:a', 'copy',
        '-f', 'mpegts', SRT_URL_SEND,
    ]
    print(f"[sender] starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def start_srt_receiver_frames():
    """ffmpeg reads SRT stream, emits one JPEG every 0.55s to LIVE_FRAMES_DIR."""
    frame_pattern = str(LIVE_FRAMES_DIR / 'f_%05d.jpg')
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-i', SRT_URL_RECV,
        '-vf', f'fps=1/{SAMPLE_INTERVAL_S},scale=960:540',
        '-q:v', '4',
        '-start_number', '1',
        '-y', frame_pattern,
    ]
    print(f"[receiver] starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---- live loop ----

def main():
    # clean frames dir
    for f in LIVE_FRAMES_DIR.glob('f_*.jpg'):
        f.unlink()

    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    roster_names = set(roster_by_short.keys())
    # include family names (Doekhi from "Doekhi, Danilho" etc.)
    for p in ctx['roster']:
        for part in re.split(r'[\s,-]+', p['name']):
            if part[:1].isupper() and len(part) > 2:
                roster_names.add(part.strip(',.'))
    print(f"model={MODEL} rich_ctx={len(rich_ctx)} chars; roster={len(roster_names)}")

    # Start SRT receiver FIRST (listener), then sender
    # Actually the sender is the listener here (SRT server); receiver is caller.
    sender_proc = start_srt_sender()
    time.sleep(1.5)  # let sender come up
    recv_proc = start_srt_receiver_frames()

    accepted = []
    all_attempts = []
    subs = []
    booth_busy_until_wall = 0.0
    booth_busy_until_video = 0.0
    wall_start = time.monotonic()

    def video_time_of_frame(idx):
        # frame N was captured at video_time = N * SAMPLE_INTERVAL_S
        return idx * SAMPLE_INTERVAL_S

    def wall_time():
        return time.monotonic() - wall_start

    # Two audio buffers for EN + FR
    audio_en = bytearray(int((DURATION_S + 30) * SR_TTS * 2))
    audio_fr = bytearray(int((DURATION_S + 30) * SR_TTS * 2))
    audio_lock = threading.Lock()
    completed_lines = []

    # Executor for TTS
    tts_exec = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix='tts')

    def enqueue_tts_and_write(text, video_t, wall_t_when_accepted, fr_pretranslated=None):
        """Fire EN + FR TTS in parallel, write pcm into audio buffers when done.

        If fr_pretranslated is provided (v19 polisher path), skip the translate
        call entirely.
        """
        tag = pick_tag(text)
        tagged_en = f"{tag} {text}"
        if fr_pretranslated:
            fr = fr_pretranslated
        else:
            try:
                fr = translate_fr(text)
            except Exception as e:
                print(f"    translate fail: {e}")
                fr = text
        tagged_fr = f"{tag} {fr}"

        def one_tts(track_name, tagged, voice, out_buf):
            t0 = time.monotonic()
            try:
                pcm = tts(tagged, voice)
            except Exception as e:
                print(f"    tts {track_name} fail: {e}")
                return None
            tts_ms = int((time.monotonic()-t0)*1000)
            # position at video_t + natural lag
            start_s = video_t + NATURAL_LAG_S
            b = int(start_s * SR_TTS) * 2
            with audio_lock:
                if b < len(out_buf):
                    usable = min(len(pcm), len(out_buf) - b)
                    if usable > 0:
                        out_buf[b:b+usable] = pcm[:usable]
            return tts_ms

        f_en = tts_exec.submit(one_tts, 'EN', tagged_en, EN_VOICE, audio_en)
        f_fr = tts_exec.submit(one_tts, 'FR', tagged_fr, FR_VOICE, audio_fr)
        en_ms = f_en.result()
        fr_ms = f_fr.result()
        completed_lines.append({
            'video_time_s': round(video_t, 2),
            'wall_at_accept_s': round(wall_t_when_accepted, 2),
            'text': text, 'fr': fr, 'tag': tag,
            'natural_start_s': round(video_t + NATURAL_LAG_S, 2),
            'en_tts_ms': en_ms, 'fr_tts_ms': fr_ms,
        })
        print(f"    tts done EN={en_ms}ms FR={fr_ms}ms  → {text[:60]!r}")

    # Main loop: watch frames dir; when we have CONTEXT_FRAMES available AND
    # booth is free, fire vision call.
    processed_idx = 0
    max_wait_s = 400  # give up after this many seconds of no new frames
    last_new_frame_wall = wall_time()
    stopping = False

    while True:
        # 1. Enumerate available frames
        current_frames = sorted(LIVE_FRAMES_DIR.glob('f_*.jpg'))
        n = len(current_frames)
        if n > processed_idx:
            last_new_frame_wall = wall_time()
        processed_idx = n

        # 2. Are we at end? sender died AND no new frames for a while
        sender_done = sender_proc.poll() is not None
        if sender_done and (wall_time() - last_new_frame_wall) > 5:
            print(f"[main] sender ended; waiting {(wall_time() - last_new_frame_wall):.1f}s since last frame; stopping")
            stopping = True

        # 3. If we have enough frames for a burst AND booth is free, process the newest burst
        if n >= CONTEXT_FRAMES:
            latest_frame_idx = n  # 1-indexed
            latest_video_t = video_time_of_frame(latest_frame_idx)
            # Optional anchor frames from further back (env-var override)
            anchor_offsets_s = os.environ.get('AI_COMMENTATOR_ANCHOR_OFFSETS_S', '')
            anchor_paths = []
            if anchor_offsets_s:
                for off_str in anchor_offsets_s.split(','):
                    off = float(off_str)
                    anchor_t = latest_video_t - off
                    if anchor_t <= 0: continue
                    anchor_idx = int(round(anchor_t / SAMPLE_INTERVAL_S)) - 1
                    if 0 <= anchor_idx < n:
                        anchor_paths.append(current_frames[anchor_idx])
            burst_paths = anchor_paths + current_frames[max(0, n - CONTEXT_FRAMES - 1) : n]  # anchors + burst + carry
            wt = wall_time()
            if wt >= booth_busy_until_wall and latest_video_t >= booth_busy_until_video:
                prev_texts = [a['text'] for a in accepted[-12:]]
                alias_usage = summarise_alias_usage(prev_texts, aliases)
                referee_usage = summarise_referee_usage(prev_texts)
                sub_hist = format_sub_history(subs)
                pitch_state = format_pitch_state(ctx['roster'], subs)
                # CPU-side motion detector — the LLM never sees this signal.
                # If it says REPLAY, we short-circuit: canned line, skip vision + polisher.
                motion_val, motion_level, _ = compute_motion_hint(burst_paths)
                if motion_level == 'replay':
                    n_replay = sum(1 for a in accepted if a.get('replay'))
                    variants = [
                        "And here's the replay of that moment.",
                        "A closer look at what just happened.",
                        "The replay shows it again.",
                        "Watch that one back.",
                    ]
                    text = variants[n_replay % len(variants)]
                    vision_ms = 0
                    err = None
                    is_replay = True
                    motion_signal = 'replay'
                    wt2 = wall_time()
                    print(f"[t_wall={wt:5.1f}s / t_video={latest_video_t:5.1f}s]  motion=replay (CPU) → canned line, skipping vision")
                else:
                    prompt = build_prompt(rich_ctx, latest_video_t, prev_texts,
                                          alias_usage, sub_hist, pitch_state,
                                          referee_usage=referee_usage)
                    print(f"[t_wall={wt:5.1f}s / t_video={latest_video_t:5.1f}s] burst idx={latest_frame_idx} frames={len(burst_paths)}  motion={motion_level}  calling vision...")
                    text, vision_ms, err = vision_call(burst_paths, prompt)
                    wt2 = wall_time()
                    is_replay = False
                    motion_signal = motion_level
                if text and text.lower().startswith('motion:'):
                    first, _, rest = text.partition('\n')
                    motion_signal = first.split(':', 1)[1].strip().lower()  # high|normal|low|replay
                    text = rest.lstrip('\n').strip()
                    if motion_signal == 'replay':
                        is_replay = True
                        n_replay = sum(1 for a in accepted if a.get('replay'))
                        variants = [
                            "And here's the replay of that moment.",
                            "A closer look at what just happened.",
                            "The replay shows it again.",
                            "Watch that one back.",
                        ]
                        text = variants[n_replay % len(variants)]
                elif text and text.lower().startswith('replay:'):
                    first, _, rest = text.partition('\n')
                    is_replay = 'yes' in first.lower()
                    text = rest.strip()
                    if is_replay:
                        n_replay = sum(1 for a in accepted if a.get('replay'))
                        variants = [
                            "And here's the replay of that moment.",
                            "A closer look at what just happened.",
                            "The replay shows it again.",
                            "Watch that one back.",
                        ]
                        text = variants[n_replay % len(variants)]

                # ---- v19/v20 two-stage: if safe_draft mode AND not a replay, polish ----
                fr_pretranslated = None
                polisher_ms = 0
                if PROMPT_STYLE == 'safe_draft' and text and not is_no_call(text) and not is_replay:
                    safe_draft = text
                    polished_en, polished_fr, polisher_ms = polish_line(safe_draft, [a['text'] for a in accepted[-12:]])
                    # Strip any residual REPLAY: prefix the polisher may have echoed
                    if polished_en.lower().startswith('replay:'):
                        _, _, rest = polished_en.partition('\n')
                        polished_en = rest.strip() or safe_draft
                    if polished_fr.lower().startswith('replay:'):
                        _, _, rest = polished_fr.partition('\n')
                        polished_fr = rest.strip()
                    print(f"    safe_draft ({vision_ms}ms): {safe_draft[:60]!r}")
                    print(f"    polished  ({polisher_ms}ms): {polished_en[:60]!r}")
                    text = polished_en if polished_en else safe_draft
                    fr_pretranslated = polished_fr
                if err:
                    print(f"    err {err[:120]!r}")
                elif not text or is_no_call(text):
                    print(f"    NO_CALL ({vision_ms}ms)")
                elif is_repetitive_trigram(text, [a['text'] for a in accepted], last_n=5):
                    print(f"    trigram_dup: {text[:60]!r}")
                elif is_repeated_waiting(text, [a['text'] for a in accepted]):
                    print(f"    waiting_dup: {text[:60]!r}")
                else:
                    sub = detect_sub(text, roster_by_short)
                    if sub:
                        on_pitch = {p['short_name'] for p in ctx['roster'] if p['role']=='starter'}
                        for s in subs:
                            on_pitch.discard(s['off']); on_pitch.add(s['on'])
                        if sub[0] not in on_pitch or sub[1] in on_pitch or any(s['off']==sub[0] and s['on']==sub[1] for s in subs):
                            print(f"    sub reject: {sub} ({vision_ms}ms) {text[:60]!r}")
                            all_attempts.append({'video_time_s':latest_video_t,'text':text,'reason':'sub_reject','vision_ms':vision_ms})
                            time.sleep(0.05); continue
                        subs.append({'off':sub[0],'on':sub[1],'at_s':round(latest_video_t,1)})

                    words = len(text.split())
                    est_duration_s = max(1.2, words / 3.0)
                    est_tag = cheap_tag_guess(text)
                    gate_s = 4.0 if est_tag in ('[calm]','[flatly]','[deadpan]','[resigned tone]') else 1.8
                    accepted.append({'text': text, 'video_time_s': latest_video_t,
                                     'wall_at_accept_s': wt2, 'vision_ms': vision_ms,
                                     'est_duration_s': est_duration_s})
                    booth_busy_until_video = latest_video_t + NATURAL_LAG_S + est_duration_s + (gate_s - 1.8)
                    booth_busy_until_wall = wt2 + 0.5  # small guard between calls
                    print(f"    ACCEPT (vision {vision_ms}ms) tag={est_tag} → {text[:60]!r}")
                    tts_exec.submit(enqueue_tts_and_write, text, latest_video_t, wt2, fr_pretranslated)
                all_attempts.append({'video_time_s':latest_video_t,'text':text,'vision_ms':vision_ms,'err':err})

        # 4. Stop condition: sender done, no new frames for a while, all TTS submitted done
        if stopping:
            print(f"[main] stopping loop; {len(accepted)} accepted, waiting for TTS to drain")
            tts_exec.shutdown(wait=True)
            break

        # 5. Timeout: if nothing new for too long, give up
        if wall_time() - last_new_frame_wall > max_wait_s:
            print(f"[main] no frames for {max_wait_s}s; giving up")
            tts_exec.shutdown(wait=True)
            break

        time.sleep(0.1)

    # Clean up ffmpeg subprocesses
    for p in (sender_proc, recv_proc):
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill()

    # Write outputs
    out_en_wav = BASE / f'ai_commentary_{OUT_PREFIX}_en_track.wav'
    out_fr_wav = BASE / f'ai_commentary_{OUT_PREFIX}_fr_track.wav'
    trim_to = int(DURATION_S * SR_TTS * 2)
    with wave.open(str(out_en_wav), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS); w.writeframes(bytes(audio_en[:trim_to]))
    with wave.open(str(out_fr_wav), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS); w.writeframes(bytes(audio_fr[:trim_to]))

    with open(BASE / f'commentary_{OUT_PREFIX}.jsonl', 'w') as f:
        for r in completed_lines:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    print(f"\n=== SUMMARY ===")
    print(f"Wall time to complete: {wall_time():.1f}s (source video is {DURATION_S:.0f}s)")
    print(f"Accepted lines: {len(accepted)}   Subs: {subs}")
    if accepted:
        lags = [max(0, a['wall_at_accept_s'] - a['video_time_s']) for a in accepted]
        lags.sort()
        p50 = lags[len(lags)//2] if lags else 0
        p90 = lags[int(len(lags)*0.9)] if lags else 0
        print(f"Live pipeline lag  (wall - video)  p50={p50:.2f}s  p90={p90:.2f}s")
    print(f"EN track: {out_en_wav}   FR track: {out_fr_wav}")


REPLAY_VARIANTS = [
    "And here's the replay of that moment.",
    "A closer look at what just happened.",
    "The replay shows it again.",
    "Watch that one back.",
]


def main_parallel():
    """Parallel variant of main().

    The sequential main() blocks the loop on each `vision_call`; under CPU
    contention (few cores + ffmpeg + TTS threads) a single call can stall ~30s
    and freeze the whole pipeline, starving throughput. Here vision+polish run
    in a bounded worker pool so a slow call can't freeze the loop:
      - submission is gated by video-time cadence + max in-flight
      - non-overlap between accepted lines is enforced at ACCEPT time
      - audio is placed by video-time, so out-of-order completion still lands
        each line at the correct spot in the track.
    Isolated probing (latency_probe.py) showed even 8 concurrent gpt-5.5 vision
    calls stay fast, so adding vision concurrency does not itself add latency.
    """
    for f in LIVE_FRAMES_DIR.glob('f_*.jpg'):
        f.unlink()

    ctx = build_match_context()
    rich_ctx = build_rich_context_text(ctx)
    aliases = ctx['aliases']
    roster_by_short = {p['short_name']: p for p in ctx['roster']}
    print(f"[PARALLEL] model={MODEL} concurrency={VISION_CONCURRENCY} rich_ctx={len(rich_ctx)} chars")

    sender_proc = start_srt_sender()
    time.sleep(1.5)
    recv_proc = start_srt_receiver_frames()

    accepted = []
    all_attempts = []
    subs = []
    booth_busy_until_video = 0.0
    wall_start = time.monotonic()

    def video_time_of_frame(idx):
        return idx * SAMPLE_INTERVAL_S

    def wall_time():
        return time.monotonic() - wall_start

    audio_en = bytearray(int((DURATION_S + 30) * SR_TTS * 2))
    audio_fr = bytearray(int((DURATION_S + 30) * SR_TTS * 2))
    audio_lock = threading.Lock()
    completed_lines = []
    tts_exec = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix='tts')
    vision_exec = concurrent.futures.ThreadPoolExecutor(max_workers=VISION_CONCURRENCY, thread_name_prefix='vis')

    def enqueue_tts_and_write(text, video_t, wall_t_when_accepted, fr_pretranslated=None):
        tag = pick_tag(text)
        tagged_en = f"{tag} {text}"
        if fr_pretranslated:
            fr = fr_pretranslated
        else:
            try:
                fr = translate_fr(text)
            except Exception as e:
                print(f"    translate fail: {e}"); fr = text
        tagged_fr = f"{tag} {fr}"

        def one_tts(track_name, tagged, voice, out_buf):
            t0 = time.monotonic()
            try:
                pcm = tts(tagged, voice)
            except Exception as e:
                print(f"    tts {track_name} fail: {e}"); return None
            tts_ms = int((time.monotonic()-t0)*1000)
            start_s = video_t + NATURAL_LAG_S
            b = int(start_s * SR_TTS) * 2
            with audio_lock:
                if b < len(out_buf):
                    usable = min(len(pcm), len(out_buf) - b)
                    if usable > 0:
                        out_buf[b:b+usable] = pcm[:usable]
            return tts_ms

        f_en = tts_exec.submit(one_tts, 'EN', tagged_en, EN_VOICE, audio_en)
        f_fr = tts_exec.submit(one_tts, 'FR', tagged_fr, FR_VOICE, audio_fr)
        en_ms = f_en.result(); fr_ms = f_fr.result()
        completed_lines.append({
            'video_time_s': round(video_t, 2),
            'wall_at_accept_s': round(wall_t_when_accepted, 2),
            'text': text, 'fr': fr, 'tag': tag,
            'natural_start_s': round(video_t + NATURAL_LAG_S, 2),
            'en_tts_ms': en_ms, 'fr_tts_ms': fr_ms,
        })
        print(f"    tts done EN={en_ms}ms FR={fr_ms}ms  → {text[:60]!r}")

    def vision_job(burst_paths, prompt, prev_texts, latest_video_t):
        """Runs in the worker pool: vision + (safe_draft) polish. No shared
        state mutation — returns a result dict for the main thread to finalize."""
        text, vision_ms, err = vision_call(burst_paths, prompt)
        is_replay = False
        if text and text.lower().startswith('motion:'):
            first, _, rest = text.partition('\n')
            sig = first.split(':', 1)[1].strip().lower()
            text = rest.lstrip('\n').strip()
            is_replay = (sig == 'replay')
        elif text and text.lower().startswith('replay:'):
            first, _, rest = text.partition('\n')
            is_replay = 'yes' in first.lower()
            text = rest.strip()
        fr_pre = None
        polisher_ms = 0
        if PROMPT_STYLE == 'safe_draft' and text and not is_no_call(text) and not is_replay:
            safe_draft = text
            polished_en, polished_fr, polisher_ms = polish_line(safe_draft, prev_texts)
            if polished_en.lower().startswith('replay:'):
                _, _, r = polished_en.partition('\n'); polished_en = r.strip() or safe_draft
            if polished_fr.lower().startswith('replay:'):
                _, _, r = polished_fr.partition('\n'); polished_fr = r.strip()
            text = polished_en if polished_en else safe_draft
            fr_pre = polished_fr
        return dict(text=text, vision_ms=vision_ms, err=err, polisher_ms=polisher_ms,
                    fr_pre=fr_pre, is_replay=is_replay, video_t=latest_video_t)

    def accept_line(text, video_t, vision_ms, polisher_ms, fr_pre, is_replay):
        """Main-thread only: dedup / sub / non-overlap gate / accept + TTS."""
        nonlocal booth_busy_until_video
        if is_repetitive_trigram(text, [a['text'] for a in accepted], last_n=5):
            print(f"    [{video_t:5.1f}s] trigram_dup: {text[:50]!r}"); return
        if is_repeated_waiting(text, [a['text'] for a in accepted]):
            print(f"    [{video_t:5.1f}s] waiting_dup: {text[:50]!r}"); return
        sub = detect_sub(text, roster_by_short)
        if sub:
            on_pitch = {p['short_name'] for p in ctx['roster'] if p['role'] == 'starter'}
            for s in subs:
                on_pitch.discard(s['off']); on_pitch.add(s['on'])
            if sub[0] not in on_pitch or sub[1] in on_pitch or any(s['off'] == sub[0] and s['on'] == sub[1] for s in subs):
                print(f"    [{video_t:5.1f}s] sub reject {sub}: {text[:50]!r}")
                all_attempts.append({'video_time_s': video_t, 'text': text, 'reason': 'sub_reject'}); return
            subs.append({'off': sub[0], 'on': sub[1], 'at_s': round(video_t, 1)})
        words = len(text.split())
        est_duration_s = max(1.2, words / 3.0)
        est_tag = cheap_tag_guess(text)
        gate_s = 4.0 if est_tag in ('[calm]', '[flatly]', '[deadpan]', '[resigned tone]') else 1.8
        wt2 = wall_time()
        accepted.append({'text': text, 'video_time_s': video_t, 'wall_at_accept_s': wt2,
                         'vision_ms': vision_ms, 'est_duration_s': est_duration_s, 'replay': is_replay})
        booth_busy_until_video = video_t + NATURAL_LAG_S + est_duration_s + (gate_s - 1.8)
        print(f"    [{video_t:5.1f}s] ACCEPT (vis {vision_ms}ms pol {polisher_ms}ms) tag={est_tag} → {text[:55]!r}")
        tts_exec.submit(enqueue_tts_and_write, text, video_t, wt2, fr_pre)

    def handle_result(res):
        text = res['text']; video_t = res['video_t']
        if res['err']:
            print(f"    [{video_t:5.1f}s] err {res['err'][:100]!r}")
            all_attempts.append({'video_time_s': video_t, 'err': res['err']}); return
        if res['is_replay']:
            n_replay = sum(1 for a in accepted if a.get('replay'))
            text = REPLAY_VARIANTS[n_replay % len(REPLAY_VARIANTS)]
        if not text or is_no_call(text):
            print(f"    [{video_t:5.1f}s] NO_CALL ({res['vision_ms']}ms)"); return
        if video_t < booth_busy_until_video - 0.01:
            print(f"    [{video_t:5.1f}s] drop-overlap (booth busy to {booth_busy_until_video:.1f}s): {text[:45]!r}"); return
        accept_line(text, video_t, res['vision_ms'], res['polisher_ms'], res['fr_pre'], res['is_replay'])

    pending = []
    last_submit_video_t = -999.0
    MIN_SUBMIT_SPACING_S = 1.5
    processed_idx = 0
    max_wait_s = 400
    last_new_frame_wall = wall_time()
    stopping = False

    while True:
        current_frames = sorted(LIVE_FRAMES_DIR.glob('f_*.jpg'))
        n = len(current_frames)
        if n > processed_idx:
            last_new_frame_wall = wall_time()
        processed_idx = n

        if sender_proc.poll() is not None and (wall_time() - last_new_frame_wall) > 5:
            if not stopping:
                print(f"[main] sender ended; {wall_time()-last_new_frame_wall:.1f}s since last frame; stop submitting")
            stopping = True

        # ---- SUBMIT (non-blocking) ----
        if not stopping and n >= CONTEXT_FRAMES:
            latest_frame_idx = n
            latest_video_t = video_time_of_frame(latest_frame_idx)
            inflight = sum(1 for p in pending if not p.done())
            submit_after = max(last_submit_video_t + MIN_SUBMIT_SPACING_S, booth_busy_until_video)
            if latest_video_t >= submit_after and inflight < VISION_CONCURRENCY:
                burst_paths = current_frames[max(0, n - CONTEXT_FRAMES - 1): n]
                prev_texts = [a['text'] for a in accepted[-12:]]
                # Replay detection REMOVED — the CPU slow-mo short-circuit
                # false-fired on pre-kick pauses (a player pausing before a
                # kick read as a replay). Always run the vision call now.
                alias_usage = summarise_alias_usage(prev_texts, aliases)
                referee_usage = summarise_referee_usage(prev_texts)
                sub_hist = format_sub_history(subs)
                pitch_state = format_pitch_state(ctx['roster'], subs)
                prompt = build_prompt(rich_ctx, latest_video_t, prev_texts,
                                      alias_usage, sub_hist, pitch_state,
                                      referee_usage=referee_usage)
                fut = vision_exec.submit(vision_job, burst_paths, prompt, prev_texts, latest_video_t)
                pending.append(fut)
                last_submit_video_t = latest_video_t
                print(f"[t_wall={wall_time():5.1f}s / t_video={latest_video_t:5.1f}s] SUBMIT idx={latest_frame_idx} inflight={inflight+1}")

        # ---- REAP completed (main thread finalizes) ----
        for fut in [p for p in pending if p.done()]:
            pending.remove(fut)
            try:
                handle_result(fut.result())
            except Exception as e:
                print(f"    vision_job exception: {e}")

        # ---- Stop when sender done and all in-flight drained ----
        if stopping and not pending:
            print(f"[main] draining; {len(accepted)} accepted")
            vision_exec.shutdown(wait=True)
            tts_exec.shutdown(wait=True)
            break
        if wall_time() - last_new_frame_wall > max_wait_s:
            print(f"[main] no frames for {max_wait_s}s; giving up")
            vision_exec.shutdown(wait=True)
            tts_exec.shutdown(wait=True)
            break
        time.sleep(0.05)

    for p in (sender_proc, recv_proc):
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill()

    out_en_wav = BASE / f'ai_commentary_{OUT_PREFIX}_en_track.wav'
    out_fr_wav = BASE / f'ai_commentary_{OUT_PREFIX}_fr_track.wav'
    trim_to = int(DURATION_S * SR_TTS * 2)
    with wave.open(str(out_en_wav), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS); w.writeframes(bytes(audio_en[:trim_to]))
    with wave.open(str(out_fr_wav), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS); w.writeframes(bytes(audio_fr[:trim_to]))
    with open(BASE / f'commentary_{OUT_PREFIX}.jsonl', 'w') as f:
        for r in completed_lines:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    print(f"\n=== SUMMARY (parallel) ===")
    print(f"Wall time to complete: {wall_time():.1f}s (source video is {DURATION_S:.0f}s)")
    print(f"Accepted lines: {len(accepted)}   Subs: {subs}")
    if accepted:
        lags = sorted(max(0, a['wall_at_accept_s'] - a['video_time_s']) for a in accepted)
        vms = sorted(a['vision_ms'] for a in accepted if a['vision_ms'])
        p50 = lags[len(lags)//2]; p90 = lags[int(len(lags)*0.9)]
        print(f"Live pipeline lag (wall - video)  p50={p50:.2f}s  p90={p90:.2f}s")
        if vms:
            print(f"Vision latency  min={vms[0]}ms  median={vms[len(vms)//2]}ms  max={vms[-1]}ms  (>15s: {sum(1 for v in vms if v>15000)})")
    print(f"EN track: {out_en_wav}   FR track: {out_fr_wav}")


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--pipeline', choices=list(PIPELINE_CONFIGS.keys()), required=True,
                    help='Which pipeline to run live')
    args = ap.parse_args()
    cfg = PIPELINE_CONFIGS[args.pipeline]
    PIPELINE = args.pipeline
    MODEL = cfg['model']
    MAX_OUTPUT_TOKENS = cfg['max_output_tokens']
    PROMPT_STYLE = cfg['prompt_style']
    SRT_PORT = cfg['srt_port']
    SRT_URL_SEND = f"srt://127.0.0.1:{SRT_PORT}?mode=listener&latency=200"
    SRT_URL_RECV = f"srt://127.0.0.1:{SRT_PORT}?mode=caller&latency=200"
    LIVE_FRAMES_DIR = Path(cfg['frames_dir'])
    LIVE_FRAMES_DIR.mkdir(exist_ok=True)
    OUT_PREFIX = cfg['out_prefix']
    SAMPLE_INTERVAL_S = cfg.get('sample_interval_s', 0.55)
    CONTEXT_FRAMES = cfg.get('burst_frames', 4)
    POLISHER_MODEL = cfg.get('polisher_model', 'gpt-5.4-mini')
    VISION_CONCURRENCY = cfg.get('vision_concurrency', 4)
    parallel = cfg.get('parallel', False)
    print(f"=== live SRT — pipeline={args.pipeline} model={MODEL} prompt={PROMPT_STYLE}"
          f" polisher={POLISHER_MODEL}{' PARALLEL k='+str(VISION_CONCURRENCY) if parallel else ''}"
          f" port={SRT_PORT} prefix={OUT_PREFIX} sample_interval={SAMPLE_INTERVAL_S}s"
          f" burst_frames={CONTEXT_FRAMES} ===")
    if parallel:
        main_parallel()
    else:
        main()

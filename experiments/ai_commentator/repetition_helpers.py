"""Rotation trackers — verb + player-name + referee usage across recent lines.

Same idea as summarise_alias_usage in run_v4.py: count how many times each
lemma / player name appeared in the last N accepted lines, produce a compact
"avoid these" block for the prompt.

Also: waiting-repetition detector — if the last accepted line was of the
"X waits / X stands over the ball" shape AND the play state hasn't changed,
the runner can force NO_CALL rather than say "X still waiting" again.
"""
from __future__ import annotations
import re


# ---------- referee rotation ----------

REFEREE = {
    'name': 'Florian Exner',
    'short': 'Exner',
    # Alternates the LLM can rotate through. Both English variants and neutral
    # ones — "the referee" is always safe.
    'aliases': [
        'Exner',
        'Mr Exner',
        'the referee',
        'the official',
        'the whistle',
        'Florian Exner',
    ],
}


def summarise_referee_usage(recent_texts, window=4):
    """If 'Exner' appeared naked in >=1 of the last N lines, tell the LLM to
    rotate to one of the other referee aliases."""
    if not recent_texts:
        return "  (no recent referee refs)"
    last = recent_texts[-window:]
    exner_uses = sum(1 for line in last if re.search(r'\bExner\b', line) and 'Mr Exner' not in line)
    the_ref_uses = sum(1 for line in last if re.search(r'\bthe referee\b', line, re.I))
    the_official = sum(1 for line in last if re.search(r'\bthe official\b', line, re.I))
    counts = []
    if exner_uses: counts.append(f"'Exner'×{exner_uses}")
    if the_ref_uses: counts.append(f"'the referee'×{the_ref_uses}")
    if the_official: counts.append(f"'the official'×{the_official}")
    if not counts:
        return "  (referee not named recently — 'Exner' or 'the referee' both OK)"
    return (
        "  REFEREE: previously used " + ", ".join(counts) +
        f" in last {len(last)} lines. If you need to name the referee this turn, "
        f"pick a FRESH one from: {', '.join(REFEREE['aliases'])} — do not repeat the last used form."
    )


# ---------- waiting-line detection ----------

_WAITING_PATTERNS = [
    re.compile(r'\bwait(s|ing|ed)?\b', re.I),
    re.compile(r'\bstand(s|ing)?\s+(over|near|ready)\b', re.I),
    re.compile(r'\bready\s+(to|for)\b', re.I),
    re.compile(r'\bpoise[ds]?\s+(over|to|for)\b', re.I),
    re.compile(r'\bset(\s+for|\s+over)\b', re.I),
    re.compile(r'\bhold(s|ing)?\s+(off|onto|position)\b', re.I),
    re.compile(r'\bpause[sd]?\b', re.I),
    re.compile(r'\bstill\s+(down|waiting|holding)\b', re.I),
]


def _extract_subject(text):
    """Grab the leading capitalised noun (player name / role) — a rough
    approximation of the sentence's subject."""
    m = re.match(r'\s*([A-Z][A-Za-zà-ÿ-]+(?:\s+[A-Z][A-Za-zà-ÿ-]+)?)', text)
    return m.group(1).strip() if m else None


def is_waiting_line(text):
    if not text: return False
    return any(pat.search(text) for pat in _WAITING_PATTERNS)


def is_repeated_waiting(new_text, previous_texts):
    """True if the previous accepted line was ALSO waiting-shaped AND has the
    SAME subject as this candidate — i.e. we already said 'X is waiting'
    recently, don't repeat."""
    if not new_text or not previous_texts:
        return False
    if not is_waiting_line(new_text):
        return False
    new_subj = _extract_subject(new_text)
    if not new_subj:
        return False
    for prev in previous_texts[-2:]:  # look at last 2 accepted lines
        if is_waiting_line(prev):
            prev_subj = _extract_subject(prev)
            if prev_subj and prev_subj.lower() == new_subj.lower():
                return True
    return False

# Football commentary verbs. If a verb here appears >= 2 times in the last 6
# accepted lines, we tell the LLM to swap it out.
COMMENTARY_VERBS = {
    # ball movement
    'drives', 'driving', 'carries', 'carrying', 'strolls', 'strolling',
    'jogs', 'jogging', 'shuffles', 'sprints', 'sprinting',
    'races', 'racing', 'moves', 'moving',
    # passing
    'plays', 'passes', 'pings', 'threads', 'threading',
    'whips', 'whipping', 'delivers', 'delivering', 'crosses', 'crossing',
    'flicks', 'flicking', 'chips', 'lofts', 'slides', 'sliding',
    'switches', 'switching', 'spreads', 'spreading',
    'rolls', 'rolling', 'floats', 'floating',
    # possession phases
    'probes', 'probing', 'circulates', 'circulating', 'recycles', 'recycling',
    'resets', 'resetting', 'waits', 'waiting',
    'stands', 'standing', 'holds', 'holding', 'checks', 'checking',
    'looks', 'looking',
    # defensive
    'tackles', 'tackled', 'intercepts', 'intercepting', 'blocks', 'blocking',
    'clears', 'clearing', 'scrambles', 'scrambling', 'nicks', 'nicked',
    'wins', 'won', 'covers', 'covering', 'closes', 'closing',
    # attacking
    'shoots', 'shooting', 'strikes', 'striking', 'fires', 'firing',
    'blasts', 'blasting', 'buries', 'buried', 'volleys', 'volleyed',
    'heads', 'heading', 'nutmegs', 'nutmegged',
    # goalkeeper
    'saves', 'saved', 'denies', 'denied', 'claws', 'clawed',
    'punches', 'punched', 'catches', 'caught', 'claims', 'claimed',
    'gathers', 'gathered',
    # set piece
    'sets', 'setting', 'places', 'placing',
    # generic states
    'watches', 'watching', 'gestures', 'gesturing', 'listens', 'listening',
    'signals', 'signalling', 'points', 'pointing', 'appeals', 'appealing',
    # transitions
    'switches', 'switching', 'breaks', 'breaking', 'launches', 'launching',
}


def _tokenize(text):
    return re.sub(r'[^\w\s\']', ' ', text.lower()).split()


def summarise_verb_usage(recent_texts, verbs_over_used_threshold=2, window=6):
    """Return a compact 'avoid these overused verbs' block, or None if nothing overused.

    Args:
      recent_texts: list of the last accepted commentary lines.
      verbs_over_used_threshold: count above which a verb is 'overused'.
      window: how many recent lines to look at.
    """
    if not recent_texts:
        return "  (no recent verbs — free to choose any)"
    last = recent_texts[-window:]
    counts = {}
    for line in last:
        toks = _tokenize(line)
        for tok in toks:
            if tok in COMMENTARY_VERBS:
                counts[tok] = counts.get(tok, 0) + 1
    overused = sorted([(v, c) for v, c in counts.items() if c >= verbs_over_used_threshold],
                      key=lambda x: -x[1])
    if not overused:
        return "  (verbs varied — none overused)"
    return "  AVOID these verbs this turn — used " + ", ".join(f"{v!r}×{c}" for v, c in overused) + f" in last {len(last)} lines."


def summarise_player_usage(recent_texts, roster_names, over_used_threshold=3, window=6):
    """Return a compact 'avoid these players' block, or None if nothing overused."""
    if not recent_texts:
        return "  (no recent player mentions)"
    last = recent_texts[-window:]
    counts = {}
    for line in last:
        for name in roster_names:
            # word-boundary match, case insensitive
            if re.search(rf'\b{re.escape(name)}\b', line, re.I):
                counts[name] = counts.get(name, 0) + 1
    overused = sorted([(n, c) for n, c in counts.items() if c >= over_used_threshold],
                      key=lambda x: -x[1])
    if not overused:
        return "  (player mentions varied — none overused)"
    return "  AVOID naming these players this turn — mentioned " + \
           ", ".join(f"{n}×{c}" for n, c in overused) + f" in last {len(last)} lines. Reach for OTHER players in the frame if any."

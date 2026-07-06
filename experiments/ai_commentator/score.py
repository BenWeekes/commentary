#!/usr/bin/env python3
"""Compute deterministic metrics for an AI commentator variant + write to leaderboard.

Usage:
  python score.py <variant_name> <scheduled_jsonl> [--notes "..."]

Metrics computed:
  1. lines_per_5min         — accepted count
  2. mean_gap_s             — avg gap between adjacent lines
  3. std_gap_s              — std-dev of gaps
  4. trigram_repeat_rate    — fraction of lines sharing any 3-gram with prev 5
  5. type_token_ratio       — distinct words / total words (vocabulary richness)
  6. alias_entropy          — Shannon entropy of team-reference distribution
  7. player_name_density    — mean #player-names per line (from match roster)
  8. action_verb_density    — mean #action verbs per line
  9. mainz_mentions         — count of "Mainz"
 10. union_mentions         — count of "Union"
 11. avg_words_per_line     — mean line length
 12. distinct_word_count    — total unique words

Soniox gold is included as the reference row.
"""
from __future__ import annotations
import json, sys, math, re, argparse
from collections import Counter
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
LEADERBOARD = BASE / 'leaderboard.json'

ACTION_VERBS = set("""
drives driving drove driven races racing breaks breaking broke breakaway
sprints sprinting sprinted strolls strolling strolled jogs jogging jogged
denies denied denying saves saved saving punches punched
catches caught claimed claims claiming
whips whipping whipped delivers delivered delivering
buries buried buries probes probing probed swings swung swinging
crosses crossed crossing dribbles dribbling dribbled
shoots shooting shot fires fired firing slots slotted slotting
strikes struck striking blasts blasted blasting hammers hammered
nicks nicked nipped clear cleared clearing scrambles scrambled scrambling
heads headed heading flicks flicked flicking volleys volleyed volleying
nutmegs nutmegged tackles tackled tackling
intercepts intercepted intercepting blocks blocked blocking
pings pinged pinging launches launched launching
threads threaded threading curls curled curling skids skidded skidding
recycles recycled recycling resets resetting drives drove
trundles trundled trundling thunders thundered thundering
fouls fouled fouling commits committed committing
mishits mishit miscued miscues miscuing
""".split())


def load_jsonl(path):
    out = []
    for line in open(path):
        if line.strip():
            out.append(json.loads(line))
    return out


def load_team_aliases():
    path = Path('/home/ubuntu/commentary/match_data/m05_uni_md33/team_aliases.yaml')
    if not path.exists():
        return {}
    import sys as _sys
    _sys.path.insert(0, str(BASE))
    from run_v4 import _load_team_aliases
    return _load_team_aliases() or {}


def load_roster():
    path = '/home/ubuntu/commentary/match_data/m05_uni_md33/roster.json'
    text = json.load(open(path))['roster_text']
    names = set()
    for line in text.splitlines():
        m = re.match(r'#\S+\s+(.+)', line.strip())
        if m:
            full = m.group(1).strip()
            short = full.split(',')[0].strip() if ',' in full else full
            names.add(short)
            # also include the part-name (e.g. "Lee Jae-sung", "da Costa")
            for part in re.split(r'[\s,-]+', full):
                if part[0:1].isupper() and len(part) > 2:
                    names.add(part.strip(',.'))
    return names


def tokens(text):
    return re.sub(r'[^\w\s]', ' ', text.lower()).split()


def trigrams(text):
    ws = tokens(text)
    return {tuple(ws[i:i+3]) for i in range(len(ws)-2)} if len(ws) >= 3 else set()


def shannon_entropy(counts):
    total = sum(counts.values())
    if not total: return 0.0
    h = 0.0
    for c in counts.values():
        if c > 0:
            p = c / total
            h -= p * math.log2(p)
    return h


def get_text(row):
    return row.get('text') or row.get('fr') or ''


def get_time(row):
    return row.get('scheduled_start_s') or row.get('natural_start_s') or row.get('video_time_s') or row.get('start_s') or 0


def score_variant(name, rows, aliases, roster_names):
    texts = [get_text(r) for r in rows]
    times = [get_time(r) for r in rows]
    n = len(rows)

    # Gaps
    gaps = []
    for i in range(1, len(times)):
        g = times[i] - times[i-1]
        if g > 0: gaps.append(g)
    mean_gap = sum(gaps)/len(gaps) if gaps else 0
    std_gap = (sum((g-mean_gap)**2 for g in gaps)/len(gaps))**0.5 if gaps else 0

    # Trigram repetition
    dup_count = 0
    tris = [trigrams(t) for t in texts]
    for i, tri in enumerate(tris):
        if not tri: continue
        prev = set().union(*tris[max(0, i-5):i]) if i > 0 else set()
        if tri & prev:
            dup_count += 1
    trigram_repeat_rate = dup_count / max(1, n)

    # Type-token ratio
    all_words = []
    for t in texts:
        all_words.extend(tokens(t))
    distinct_words = len(set(all_words))
    ttr = distinct_words / max(1, len(all_words))

    # Team alias entropy
    alias_counts = Counter()
    for t in texts:
        tl = t.lower()
        for team_key, data in aliases.items():
            for cat, items in data['aliases'].items():
                for alias in items:
                    if alias.lower() in tl:
                        alias_counts[(data.get('short'), alias)] += 1
    alias_h = shannon_entropy(alias_counts)

    # Player-name density
    player_hits = 0
    for t in texts:
        for word in re.findall(r'\b[A-Z][a-zà-ÿ-]+\b', t):
            if word in roster_names:
                player_hits += 1
                break  # at most 1 per line credit
    player_name_density = player_hits / max(1, n)

    # Action verb density
    av_hits = 0
    for t in texts:
        ws = tokens(t)
        for w in ws:
            if w in ACTION_VERBS:
                av_hits += 1
    action_verb_density = av_hits / max(1, n)

    # Misc
    avg_words = sum(len(tokens(t)) for t in texts) / max(1, n)
    mainz_n = sum(t.lower().count('mainz') for t in texts)
    union_n = sum(t.lower().count('union') for t in texts)

    return {
        'variant': name,
        'lines_per_5min': n,
        'mean_gap_s': round(mean_gap, 2),
        'std_gap_s': round(std_gap, 2),
        'trigram_repeat_rate': round(trigram_repeat_rate, 3),
        'type_token_ratio': round(ttr, 3),
        'distinct_word_count': distinct_words,
        'alias_entropy_bits': round(alias_h, 2),
        'alias_distinct_count': len(alias_counts),
        'player_name_density': round(player_name_density, 3),
        'action_verb_density': round(action_verb_density, 3),
        'avg_words_per_line': round(avg_words, 1),
        'mainz_mentions': mainz_n,
        'union_mentions': union_n,
    }


def upsert_leaderboard(entry, notes=None):
    if LEADERBOARD.exists():
        board = json.load(open(LEADERBOARD))
    else:
        board = {'variants': []}
    # remove any prior entry with same variant name
    board['variants'] = [v for v in board['variants'] if v['variant'] != entry['variant']]
    if notes:
        entry['notes'] = notes
    board['variants'].append(entry)
    json.dump(board, open(LEADERBOARD, 'w'), indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('variant')
    ap.add_argument('jsonl')
    ap.add_argument('--notes', default=None)
    ap.add_argument('--gold', action='store_true', help='Score Soniox gold')
    args = ap.parse_args()

    aliases = load_team_aliases()
    roster = load_roster()
    rows = load_jsonl(args.jsonl)
    entry = score_variant(args.variant, rows, aliases, roster)
    print(json.dumps(entry, indent=2))
    upsert_leaderboard(entry, notes=args.notes)
    print(f"\nWrote {LEADERBOARD}")


if __name__ == '__main__':
    main()

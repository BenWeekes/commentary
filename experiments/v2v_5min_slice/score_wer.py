#!/usr/bin/env python3
"""Compute WER for English STT (Gemini vs Soniox) vs gold transcript on the
300-600s window of m05_uni_eval clip.

Also produces a side-by-side rendering for human inspection.
"""
import json, re, sys
from pathlib import Path

BASE = Path("/tmp/v2v_compare")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def words(s: str):
    return norm(s).split()


def edit_distance(a, b):
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b)+1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0]*len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j]+1, cur[j-1]+1, prev[j-1] + (0 if x==y else 1))
        prev = cur
    return prev[-1]


def align_edits(a, b):
    """Backtrack to recover edit operations for visualisation."""
    n, m = len(a), len(b)
    dp = [[0]*(m+1) for _ in range(n+1)]
    for i in range(n+1): dp[i][0] = i
    for j in range(m+1): dp[0][j] = j
    for i in range(1, n+1):
        for j in range(1, m+1):
            if a[i-1] == b[j-1]:
                dp[i][j] = dp[i-1][j-1]
            else:
                dp[i][j] = 1 + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    ops = []
    i, j = n, m
    while i > 0 and j > 0:
        if a[i-1] == b[j-1]:
            ops.append(('=', a[i-1], a[i-1])); i-=1; j-=1
        else:
            up, left, diag = dp[i-1][j], dp[i][j-1], dp[i-1][j-1]
            m_ = min(up, left, diag)
            if diag == m_:
                ops.append(('S', a[i-1], b[j-1])); i-=1; j-=1
            elif up == m_:
                ops.append(('D', a[i-1], None)); i-=1
            else:
                ops.append(('I', None, b[j-1])); j-=1
    while i > 0: ops.append(('D', a[i-1], None)); i -= 1
    while j > 0: ops.append(('I', None, b[j-1])); j -= 1
    ops.reverse()
    return dp[n][m], ops


def main():
    gold = open(BASE / "gold_en_300_600.txt").read()

    # Gemini transcript: concatenate all 'in' events
    gemini_events = [json.loads(l) for l in open(BASE / "gemini_events.jsonl")]
    gemini_in = "".join(e["payload"] for e in gemini_events if e["kind"] == "in")
    gemini_out = "".join(e["payload"] for e in gemini_events if e["kind"] == "out")

    # Soniox transcript
    soniox = open(BASE / "soniox_en_300_600.txt").read()

    gw = words(gold)
    print(f"Gold:    {len(gw)} words")

    print(f"\n{'provider':<12} {'words':>6} {'edits':>6} {'WER':>7}")
    print("-" * 36)
    results = {}
    for name, txt in [("gold", gold), ("soniox", soniox), ("gemini", gemini_in)]:
        w = words(txt)
        if name == "gold":
            continue
        e = edit_distance(gw, w)
        wer = e / max(1, len(gw))
        results[name] = {"words": len(w), "edits": e, "wer": wer, "text": txt}
        print(f"{name:<12} {len(w):>6} {e:>6} {wer:>7.3f}")

    # Save report
    open(BASE / "wer_report.txt", "w").write(
        f"Window: 300-600s of m05_uni_eval_25min\n"
        f"Gold words: {len(gw)}\n\n"
        + "\n".join(
            f"{n}: words={r['words']} edits={r['edits']} wer={r['wer']:.3f}"
            for n, r in results.items()
        )
    )

    # Side-by-side substitution dump for the most informative differences
    print("\n=== Soniox substitution examples (top 25) ===")
    _, ops = align_edits(gw, words(soniox))
    subs = [(a, b) for (op, a, b) in ops if op == 'S']
    from collections import Counter
    sub_count = Counter((a, b) for a, b in subs)
    for (a, b), c in sub_count.most_common(25):
        print(f"  {c}× {a!r:<25} -> {b!r}")

    print("\n=== Gemini substitution examples (top 25) ===")
    _, ops = align_edits(gw, words(gemini_in))
    subs = [(a, b) for (op, a, b) in ops if op == 'S']
    sub_count = Counter((a, b) for a, b in subs)
    for (a, b), c in sub_count.most_common(25):
        print(f"  {c}× {a!r:<25} -> {b!r}")

    # FR sample for translation comparison
    print(f"\n=== Gemini FR transcript (first 500 chars) ===")
    print(gemini_out[:500])

    open(BASE / "gemini_en_full.txt", "w").write(gemini_in)
    open(BASE / "gemini_fr_full.txt", "w").write(gemini_out)
    print(f"\nSaved: gemini_en_full.txt, gemini_fr_full.txt, wer_report.txt")


if __name__ == "__main__":
    main()

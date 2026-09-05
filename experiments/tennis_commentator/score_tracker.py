#!/usr/bin/env python3
"""Fail-closed logical tennis scoreboard tracker."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

POINTS = ("0", "15", "30", "40", "AD")
SIDES = ("far", "near")


@dataclass(frozen=True)
class Score:
    far_sets: int
    far_games: int
    far_points: str
    near_sets: int
    near_games: int
    near_points: str
    server: str

    def json(self) -> dict[str, Any]:
        return asdict(self)


INITIAL = Score(0, 0, "0", 0, 0, "0", "near")


def opponent(side: str) -> str:
    if side not in SIDES:
        raise ValueError(f"unknown court side: {side}")
    return "near" if side == "far" else "far"


def _ordinary_point_states(a: str, b: str) -> set[tuple[str, str]]:
    if a not in POINTS or b not in POINTS:
        return set()
    # Successor when the player represented by `a` wins exactly one point.
    if a == "AD":
        return {("0", "0")}
    if b == "AD":
        return {("40", "40")}
    if a == "40" and b == "40":
        return {("AD", "40")}
    if a == "40":
        return {("0", "0")}
    return {(POINTS[POINTS.index(a) + 1], b)}


def _point_successors(a: str, b: str) -> set[tuple[str, str]]:
    out = _ordinary_point_states(a, b)
    out |= {(y, x) for x, y in _ordinary_point_states(b, a)}
    return out


def is_legal_transition(old: Score, new: Score) -> bool:
    """Accept identical reads or exactly one legal point/game/set transition."""
    if new.server not in SIDES:
        return False
    if old == new:
        return True
    if min(old.far_sets, old.near_sets, old.far_games, old.near_games,
           new.far_sets, new.near_sets, new.far_games, new.near_games) < 0:
        return False
    if new.far_sets < old.far_sets or new.near_sets < old.near_sets:
        return False

    old_points = (old.far_points, old.near_points)
    new_points = (new.far_points, new.near_points)

    # One normal point, no game/set change. Server cannot change mid-game.
    if (
        new.far_sets == old.far_sets
        and new.near_sets == old.near_sets
        and new.far_games == old.far_games
        and new.near_games == old.near_games
        and new_points in _point_successors(*old_points)
        and new_points != ("0", "0")
    ):
        return new.server == old.server

    # Game completed: points reset, exactly one game rises, service changes.
    game_delta = (new.far_games - old.far_games, new.near_games - old.near_games)
    if (
        new.far_sets == old.far_sets
        and new.near_sets == old.near_sets
        and game_delta in ((1, 0), (0, 1))
        and new_points == ("0", "0")
        and ("0", "0") in _point_successors(*old_points)
    ):
        return new.server != old.server

    # Set completed: game counters and points reset; exactly one set rises.
    set_delta = (new.far_sets - old.far_sets, new.near_sets - old.near_sets)
    if (
        set_delta in ((1, 0), (0, 1))
        and new.far_games == new.near_games == 0
        and new_points == ("0", "0")
    ):
        winner_games, loser_games = (
            (old.far_games, old.near_games) if set_delta == (1, 0)
            else (old.near_games, old.far_games)
        )
        valid_set_score = (winner_games >= 6 and winner_games - loser_games >= 2) or (
            winner_games == 7 and loser_games in (5, 6)
        )
        # The terminal game transition has already alternated service. Resetting
        # the displayed games into the next set must not alternate it a second time.
        return valid_set_score and new.server == old.server
    return False


def transition_type(old: Score, new: Score) -> str:
    """Classify a committed transition without inventing an extra point."""
    if old == new:
        return "unchanged"
    if not is_legal_transition(old, new):
        return "unsupported"
    if (new.far_sets, new.near_sets) != (old.far_sets, old.near_sets):
        # This is the scoreboard resetting after a set already ended. The
        # terminal point was represented by the preceding game transition.
        return "set_reset"
    if (new.far_games, new.near_games) != (old.far_games, old.near_games):
        return "game"
    return "point"


def point_winner(old: Score, new: Score) -> str | None:
    """Return the side that won the represented point, if there is one."""
    kind = transition_type(old, new)
    if kind == "point":
        if (new.far_points, new.near_points) in _ordinary_point_states(
            old.far_points, old.near_points
        ):
            return "far"
        if (new.near_points, new.far_points) in _ordinary_point_states(
            old.near_points, old.far_points
        ):
            return "near"
    if kind == "game":
        return (
            "far"
            if new.far_games > old.far_games
            else "near"
        )
    return None


def game_winner(old: Score, new: Score) -> str | None:
    if transition_type(old, new) != "game":
        return None
    return "far" if new.far_games > old.far_games else "near"


def set_winner(old: Score, new: Score) -> str | None:
    if transition_type(old, new) != "set_reset":
        return None
    return "far" if new.far_sets > old.far_sets else "near"


def _wins_set_after_game(score: Score, side: str) -> bool:
    winner_games = (
        score.far_games + 1 if side == "far" else score.near_games + 1
    )
    loser_games = score.near_games if side == "far" else score.far_games
    return (
        winner_games >= 6 and winner_games - loser_games >= 2
    ) or (
        winner_games == 7 and loser_games in (5, 6)
    )


def pressure_state(score: Score, best_of_sets: int = 3) -> dict[str, Any]:
    """Derive only scoring stakes that follow mechanically from the board."""
    if (
        score.far_points not in POINTS
        or score.near_points not in POINTS
        or (score.far_games == 6 and score.near_games == 6)
    ):
        return {
            "supported": False,
            "kind": "tiebreak_or_unknown",
            "owner": None,
            "count": 0,
            "break_point": False,
            "set_point": False,
            "match_point": False,
        }

    if score.far_points == score.near_points == "40":
        return {
            "supported": True,
            "kind": "deuce",
            "owner": None,
            "count": 0,
            "break_point": False,
            "set_point": False,
            "match_point": False,
        }

    owner: str | None = None
    count = 0
    for side, own, other in (
        ("far", score.far_points, score.near_points),
        ("near", score.near_points, score.far_points),
    ):
        if own == "AD":
            owner, count = side, 1
            break
        if own == "40" and other in {"0", "15", "30"}:
            owner = side
            count = {"0": 3, "15": 2, "30": 1}[other]
            break

    if owner is None:
        return {
            "supported": True,
            "kind": "ordinary",
            "owner": None,
            "count": 0,
            "break_point": False,
            "set_point": False,
            "match_point": False,
        }

    break_point = owner != score.server
    set_point = _wins_set_after_game(score, owner)
    sets = score.far_sets if owner == "far" else score.near_sets
    sets_to_win = best_of_sets // 2 + 1
    match_point = set_point and sets + 1 >= sets_to_win
    return {
        "supported": True,
        "kind": "game_point",
        "owner": owner,
        "count": count,
        "break_point": break_point,
        "set_point": set_point,
        "match_point": match_point,
    }


class ScoreTracker:
    """Require two agreeing, high-confidence reads before committing a change."""

    def __init__(self, initial: Score = INITIAL, threshold: float = 0.86):
        self.current = initial
        self.threshold = threshold
        self._candidate: Score | None = None
        self._sightings = 0

    def observe(self, candidate: Score | None, confidence: float) -> dict[str, Any]:
        before = self.current
        if candidate is None or confidence < self.threshold:
            self._candidate, self._sightings = None, 0
            return {"accepted": False, "reason": "uncertain", "score": before.json()}
        if candidate == before:
            self._candidate, self._sightings = None, 0
            return {"accepted": True, "changed": False, "reason": "confirmed", "score": before.json()}
        if not is_legal_transition(before, candidate):
            self._candidate, self._sightings = None, 0
            return {"accepted": False, "reason": "illegal_transition", "score": before.json()}
        if candidate == self._candidate:
            self._sightings += 1
        else:
            self._candidate, self._sightings = candidate, 1
        if self._sightings < 2:
            return {
                "accepted": False,
                "reason": "awaiting_corroboration",
                "sightings": self._sightings,
                "score": before.json(),
            }
        self.current = candidate
        self._candidate, self._sightings = None, 0
        return {"accepted": True, "changed": True, "reason": "corroborated", "score": self.current.json()}

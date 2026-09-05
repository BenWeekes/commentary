#!/usr/bin/env python3
"""Generate deterministic tennis commentary from accepted grounded signals."""
from __future__ import annotations

import argparse
import json

from score_tracker import (
    INITIAL,
    Score,
    ScoreTracker,
    game_winner,
    point_winner,
    pressure_state,
    set_winner,
    transition_type,
)
from tennis_common import (
    ARTIFACTS,
    CONFIG,
    DELAY_S,
    OUTPUT_ARTIFACTS,
    SHARED_ARTIFACTS,
    assert_football_idle,
    read_jsonl,
)

INTRO = (
    "Daniil Glinka and Aidan Mayo begin their first-round meeting in Cary, "
    "with Mayo serving first."
)
COLOR_LINES = (
    "Glinka is the third seed in this Challenger event here in Cary.",
    "Mayo entered the Cary Challenger draw with a protected ranking.",
    "Mayo won Drummondville in 2024; Glinka took the title there in 2025.",
)
CHANGEOVER_LINE = (
    "The players change ends after Mayo's opening hold; Glinka serves next."
)
SERVICE_CONTEXT_LINE = (
    "The left-handed Glinka begins his first service game, trailing 0-1."
)

FIXED_LOCALIZATIONS = {
    INTRO: (
        "Premier tour à Cary entre Daniil Glinka et Aidan Mayo. "
        "Mayo sert en premier.",
        "Daniil Glinka e Aidan Mayo iniciam a primeira rodada em Cary, "
        "com Mayo no saque.",
    ),
    COLOR_LINES[0]: (
        "Glinka est tête de série numéro trois de ce Challenger à Cary.",
        "Glinka é o terceiro cabeça de chave neste Challenger em Cary.",
    ),
    COLOR_LINES[1]: (
        "Mayo est entré dans le tableau du Challenger de Cary avec un classement protégé.",
        "Mayo entrou na chave do Challenger de Cary com ranking protegido.",
    ),
    COLOR_LINES[2]: (
        "Mayo a gagné à Drummondville en 2024 ; Glinka y a remporté le titre "
        "en 2025.",
        "Mayo venceu em Drummondville em 2024; Glinka conquistou o título lá "
        "em 2025.",
    ),
    CHANGEOVER_LINE: (
        "Les joueurs changent de côté après le jeu de service remporté par "
        "Mayo en ouverture ; Glinka sera au service.",
        "Os jogadores trocam de lado após Mayo confirmar o saque no game "
        "inicial; Glinka saca em seguida.",
    ),
    SERVICE_CONTEXT_LINE: (
        "Le gaucher Glinka commence son premier jeu de service, mené 0-1.",
        "O canhoto Glinka começa seu primeiro game de saque, perdendo por 0 a 1.",
    ),
}

NEUTRAL_POINT = {
    "ended_in_burst": False,
    "winner": "unknown",
    "ending": "unknown",
    "confidence": 0,
}

BEST_OF_SETS = int(CONFIG["match"].get("best_of_sets", 3))


def _near_stt(rows: list[dict], t: float) -> list[str]:
    return [
        row["text"]
        for row in rows
        if float(row.get("video_time_s", -99)) <= t
        and float(row.get("end_s", row.get("video_time_s", -99))) >= t - 5
        and float(row.get("conf", 0)) >= 0.82
    ][:3]


def _score_from_detection(det: dict, current: Score) -> Score | None:
    board = det.get("scoreboard") or {}
    if not board.get("visible"):
        return None
    raw_values = [
        board.get(f"{side}_{part}")
        for side in ("far", "near")
        for part in ("sets", "games", "points")
    ]
    if all(value is None for value in raw_values):
        return None
    try:
        far_sets = (
            int(board["far_sets"])
            if board.get("far_sets") is not None
            else current.far_sets
        )
        near_sets = (
            int(board["near_sets"])
            if board.get("near_sets") is not None
            else current.near_sets
        )
        far_games = (
            int(board["far_games"])
            if board.get("far_games") is not None
            else current.far_games
        )
        near_games = (
            int(board["near_games"])
            if board.get("near_games") is not None
            else current.near_games
        )
        far_points = (
            str(board["far_points"])
            if board.get("far_points") is not None
            else current.far_points
        )
        near_points = (
            str(board["near_points"])
            if board.get("near_points") is not None
            else current.near_points
        )

        # A game count change deterministically resets points and alternates
        # service. Raw visual server guesses never mutate accepted state.
        game_changed = (far_games, near_games) != (
            current.far_games,
            current.near_games,
        )
        set_changed = (far_sets, near_sets) != (
            current.far_sets,
            current.near_sets,
        )
        if set_changed:
            if board.get("far_points") is None or board.get("near_points") is None:
                far_points = near_points = "0"
            server = current.server
        elif game_changed:
            if board.get("far_points") is None or board.get("near_points") is None:
                far_points = near_points = "0"
            server = "far" if current.server == "near" else "near"
        else:
            server = current.server

        # This scoreboard sometimes puts the game count in a blank point cell.
        # The legal-transition and corroboration checks still have to accept it.
        for side, value, opposite in (
            ("far", far_points, near_points),
            ("near", near_points, far_points),
        ):
            if (
                value.isdigit()
                and value not in {"0", "15", "30", "40"}
                and opposite == "0"
                and board.get(f"{side}_games") is None
            ):
                if side == "far":
                    far_games, far_points, near_points = int(value), "0", "0"
                else:
                    near_games, far_points, near_points = int(value), "0", "0"
                server = "far" if current.server == "near" else "near"
        return Score(
            far_sets,
            far_games,
            far_points,
            near_sets,
            near_games,
            near_points,
            server,
        )
    except (TypeError, ValueError):
        return None


def player_name(side: str) -> str:
    return "Glinka" if side == "far" else "Mayo"


def score_text(score: Score) -> str:
    return (
        f"Glinka {score.far_sets}-{score.near_sets} sets, "
        f"{score.far_games}-{score.near_games} games, "
        f"{score.far_points}-{score.near_points}; "
        f"{player_name(score.server)} serving"
    )


def court_mapping(score: Score) -> dict[str, str]:
    """Derive ends from the known initial orientation and tennis changeovers."""
    completed_games = score.far_games + score.near_games
    glinka_at_far = completed_games % 4 in {0, 3}
    return {
        "far_end": "Daniil Glinka" if glinka_at_far else "Aidan Mayo",
        "near_end": "Aidan Mayo" if glinka_at_far else "Daniil Glinka",
    }


def _server_points(score: Score) -> tuple[str, str]:
    if score.server == "far":
        return score.far_points, score.near_points
    return score.near_points, score.far_points


def _ordinary_score_lines(current: Score) -> tuple[str, str, str]:
    server = player_name(current.server)
    serving, receiving = _server_points(current)
    if serving == receiving:
        if serving == "40":
            return (
                f"Deuce on {server}'s serve.",
                f"Égalité sur le service de {server}.",
                f"Iguais no saque de {server}.",
            )
        return (
            f"{serving}-all on {server}'s serve.",
            f"{serving} partout sur le service de {server}.",
            f"{serving} iguais no saque de {server}.",
        )
    if serving == "AD":
        return (
            f"Advantage {server} on serve.",
            f"Avantage {server} au service.",
            f"Vantagem de {server} no saque.",
        )
    if receiving == "AD":
        receiver = player_name("near" if current.server == "far" else "far")
        return (
            f"Advantage {receiver} on {server}'s serve.",
            f"Avantage {receiver} sur le service de {server}.",
            f"Vantagem de {receiver} no saque de {server}.",
        )

    spoken = {"0": "love", "15": "15", "30": "30", "40": "40"}
    en = f"{server} serves at {spoken.get(serving, serving)}-{spoken.get(receiving, receiving)}"
    fr = f"{server} sert à {serving}-{receiving}"
    pt = f"{server} saca em {serving} a {receiving}"
    if serving == "40" and receiving in {"0", "15", "30"}:
        game_points = {"0": 3, "15": 2, "30": 1}[receiving]
        if game_points == 1:
            return (
                en + " — game point.",
                fr + " — balle de jeu.",
                pt + " — ponto para fechar o game.",
            )
        fr_count = "trois" if game_points == 3 else "deux"
        pt_count = "três" if game_points == 3 else "dois"
        en_count = "three" if game_points == 3 else "two"
        return (
            en + f" — {en_count} game points.",
            fr + f" — {fr_count} balles de jeu.",
            pt + f" — {pt_count} pontos para fechar o game.",
        )
    return en + ".", fr + ".", pt + "."


def _score_display(score: Score) -> str:
    serving, receiving = _server_points(score)
    if serving == receiving:
        return "love-all" if serving == "0" else f"{serving}-all"
    spoken = {"0": "love", "15": "15", "30": "30", "40": "40", "AD": "advantage"}
    return f"{spoken.get(serving, serving)}-{spoken.get(receiving, receiving)}"


def _numeric_score_display(score: Score) -> str:
    serving, receiving = _server_points(score)
    return f"{serving}-{receiving}"


def _pt_spoken_score(score: Score) -> str:
    serving, receiving = _server_points(score)
    spoken = {
        "0": "zero",
        "15": "quinze",
        "30": "trinta",
        "40": "quarenta",
        "AD": "vantagem",
    }
    return f"{spoken.get(serving, serving)} a {spoken.get(receiving, receiving)}"


def _fr_spoken_score(score: Score) -> str:
    serving, receiving = _server_points(score)
    spoken = {
        "0": "zéro",
        "15": "quinze",
        "30": "trente",
        "40": "quarante",
        "AD": "avantage",
    }
    return f"{spoken.get(serving, serving)} à {spoken.get(receiving, receiving)}"


def _state_phase(score: Score, point_number: int, kind: str) -> str:
    if kind == "game":
        return "game_complete"
    pressure = pressure_state(score, BEST_OF_SETS)
    if pressure["kind"] in {"game_point", "deuce"}:
        return "pressure"
    if point_number <= 1:
        return "opening_point"
    return "developing_game"


def build_score_intent(
    previous: Score,
    current: Score,
    point_winners: list[str],
) -> dict:
    """Turn one accepted transition into an auditable semantic instruction."""
    kind = transition_type(previous, current)
    winner = point_winner(previous, current)
    pressure_before = pressure_state(previous, BEST_OF_SETS)
    pressure_after = pressure_state(current, BEST_OF_SETS)
    streak = 0
    if winner:
        for side in reversed(point_winners):
            if side != winner:
                break
            streak += 1
    point_number = len(point_winners)
    code = kind
    if kind == "set_reset":
        winner = set_winner(previous, current)
        code = "set_complete"
    elif kind == "game":
        winner = game_winner(previous, current)
        code = "hold" if winner == previous.server else "break"
    elif kind == "point":
        if (
            pressure_before["kind"] == "game_point"
            and pressure_before["owner"] != winner
            and pressure_after["kind"] == "game_point"
            and pressure_after["owner"] == pressure_before["owner"]
        ):
            code = "game_point_saved"
        elif point_number == 1:
            code = "opening_point"
        elif pressure_after["kind"] == "game_point":
            code = (
                "break_points"
                if pressure_after["break_point"]
                else "game_points"
            )
        elif current.far_points == current.near_points:
            code = "receiver_answers"
        elif winner == current.server and streak >= 2:
            code = "server_run"
        elif winner == current.server:
            code = "server_ahead"
        else:
            code = "receiver_ahead"
    state_phase = _state_phase(current, point_number, kind)
    return {
        "type": "score_outcome",
        "code": code,
        "transition": kind,
        "winner": winner,
        "server": current.server if kind != "game" else previous.server,
        "next_server": current.server,
        "point_number_in_game": point_number,
        "winner_streak": streak,
        "score_before": previous.json(),
        "score_after": current.json(),
        "pressure_before": pressure_before,
        "pressure_after": pressure_after,
        "state_phase": state_phase,
        "evidence": ["corroborated_score_transition", "tennis_scoring_rules"],
        "policy": {
            "decision": "emit",
            "reason": f"accepted_{kind}_outcome",
            "priority": "high" if state_phase in {"pressure", "game_complete"} else "medium",
        },
    }


def render_score_intent(intent: dict) -> tuple[str, str, str]:
    """Render only meanings encoded in the structured score intent."""
    code = intent["code"]
    winner = player_name(intent["winner"]) if intent.get("winner") else None
    server = player_name(intent["server"])
    next_server = player_name(intent["next_server"])
    current = Score(**intent["score_after"])
    display = _score_display(current)
    pressure = intent["pressure_after"]
    point_number = int(intent["point_number_in_game"])
    streak = int(intent["winner_streak"])

    if code == "opening_point":
        game_qualifier = (
            " of his first service game"
            if current.far_games + current.near_games == 1
            else ""
        )
        fr_qualifier = (
            " de son premier jeu de service"
            if current.far_games + current.near_games == 1
            else ""
        )
        pt_qualifier = (
            " do seu primeiro game de saque"
            if current.far_games + current.near_games == 1
            else ""
        )
        return (
            f"{winner} wins the opening point{game_qualifier}"
            f"{'' if game_qualifier else ' of the match on serve'}, {display}.",
            f"{winner} remporte le premier point{fr_qualifier}"
            f"{'' if fr_qualifier else ' au service'}, 15-0.",
            f"{winner} vence o primeiro ponto{pt_qualifier}"
            f"{'' if pt_qualifier else ' no saque'}, 15 a 0.",
        )
    if code == "server_run":
        en_count = {2: "Two", 3: "Three"}.get(streak, str(streak))
        fr_count = {2: "Deux", 3: "Trois"}.get(streak, str(streak))
        pt_count = {2: "Dois", 3: "Três"}.get(streak, str(streak))
        return (
            f"{winner} takes {en_count.lower()} straight points on serve to "
            f"open the game, {display}.",
            f"{fr_count} points de suite pour {winner} au service, "
            f"{current.near_points}-{current.far_points}.",
            f"{pt_count} pontos seguidos para {winner} no saque, "
            f"{current.near_points} a {current.far_points}.",
        )
    if code in {"game_points", "break_points"}:
        count = int(pressure["count"])
        en_count = {1: "one", 2: "two", 3: "three"}[count]
        fr_count = {1: "une", 2: "deux", 3: "trois"}[count]
        pt_count = {1: "um", 2: "dois", 3: "três"}[count]
        en_points = {2: "two", 3: "three", 4: "four"}.get(
            point_number, str(point_number)
        )
        fr_points = {2: "deux", 3: "trois", 4: "quatre"}.get(
            point_number, str(point_number)
        )
        pt_points = {2: "dois", 3: "três", 4: "quatro"}.get(
            point_number, str(point_number)
        )
        if pressure["match_point"]:
            en_stake = "match point" + ("s" if count > 1 else "")
            fr_stake = "balle de match" if count == 1 else "balles de match"
            pt_stake = "match point" if count == 1 else "match points"
        elif pressure["set_point"]:
            en_stake = "set point" + ("s" if count > 1 else "")
            fr_stake = "balle de set" if count == 1 else "balles de set"
            pt_stake = "set point" if count == 1 else "set points"
        elif pressure["break_point"]:
            en_stake = "break point" + ("s" if count > 1 else "")
            fr_stake = "balle de break" if count == 1 else "balles de break"
            pt_stake = "break point" if count == 1 else "break points"
        else:
            en_stake = "game point" + ("s" if count > 1 else "")
            fr_stake = "balle de jeu" if count == 1 else "balles de jeu"
            pt_stake = (
                "ponto para fechar o game"
                if count == 1
                else "pontos para fechar o game"
            )
        en_context = "on return" if pressure["break_point"] else "on serve"
        fr_context = "en retour" if pressure["break_point"] else "au service"
        pt_context = "na devolução" if pressure["break_point"] else "no saque"
        return (
            f"{winner} takes the first {en_points} points {en_context} — "
            f"{en_count} {en_stake} at {display}.",
            f"{winner} remporte les {fr_points} premiers points {fr_context} — "
            f"{fr_count} {fr_stake} à {_numeric_score_display(current)}.",
            f"{winner} vence os primeiros {pt_points} pontos {pt_context} — "
            f"{pt_count} {pt_stake} em "
            f"{_numeric_score_display(current).replace('-', ' a ')}.",
        )
    if code == "game_point_saved":
        owner = player_name(pressure["owner"])
        count = int(pressure["count"])
        en_count = {1: "one", 2: "two", 3: "three"}[count]
        fr_count = {1: "une", 2: "deux", 3: "trois"}[count]
        pt_count = {1: "um", 2: "dois", 3: "três"}[count]
        if pressure["match_point"]:
            en_stake = "match point" + ("s" if count > 1 else "")
            fr_stake = "balle de match" if count == 1 else "balles de match"
            pt_stake = "match point" if count == 1 else "match points"
        elif pressure["set_point"]:
            en_stake = "set point" + ("s" if count > 1 else "")
            fr_stake = "balle de set" if count == 1 else "balles de set"
            pt_stake = "set point" if count == 1 else "set points"
        elif pressure["break_point"]:
            en_stake = "break point" + ("s" if count > 1 else "")
            fr_stake = "balle de break" if count == 1 else "balles de break"
            pt_stake = "break point" if count == 1 else "break points"
        else:
            en_stake = "game point" + ("s" if count > 1 else "")
            fr_stake = "balle de jeu" if count == 1 else "balles de jeu"
            pt_stake = (
                "ponto para fechar o game"
                if count == 1
                else "pontos para fechar o game"
            )
        en_context = " on serve" if not pressure["break_point"] else ""
        fr_context = " au service" if not pressure["break_point"] else ""
        pt_context = " no saque" if not pressure["break_point"] else ""
        return (
            f"{winner} saves one, but {owner} still has {en_count} "
            f"{en_stake}{en_context} at {display}.",
            f"{winner} en sauve une, mais {owner} a encore {fr_count} "
            f"{fr_stake}{fr_context} à {_numeric_score_display(current)}.",
            f"{winner} salva um, mas {owner} ainda tem {pt_count} "
            f"{pt_stake}{pt_context} em "
            f"{_numeric_score_display(current).replace('-', ' a ')}.",
        )
    if code in {"hold", "break"}:
        opening = (
            current.far_games + current.near_games == 1
            and current.far_sets + current.near_sets == 0
        )
        if code == "hold":
            return (
                f"{winner} holds in the {'opening game' if opening else 'game'}. "
                f"{next_server} will serve at "
                f"{current.far_games}-{current.near_games}.",
                f"{winner} tient son service dans "
                f"{'le premier jeu' if opening else 'le jeu'}. "
                f"{next_server} servira à {current.far_games}-{current.near_games}.",
                f"{winner} confirma o saque no "
                f"{'game inicial' if opening else 'game'}. "
                f"{next_server} saca em {current.far_games} a {current.near_games}.",
            )
        return (
            f"{winner} breaks serve. {next_server} will serve at "
            f"{current.far_games}-{current.near_games}.",
            f"{winner} prend le service adverse. {next_server} servira à "
            f"{current.far_games}-{current.near_games}.",
            f"{winner} quebra o saque. {next_server} saca em "
            f"{current.far_games} a {current.near_games}.",
        )
    if code == "receiver_answers":
        return (
            f"{winner} answers to make it {display} on {server}'s serve.",
            f"{winner} réplique : {_server_points(current)[0]} partout sur le service "
            f"de {server}.",
            f"{winner} responde e deixa {_server_points(current)[0]} iguais no saque "
            f"de {server}.",
        )
    if code == "server_ahead":
        if current.far_games + current.near_games == 1 and point_number == 3:
            return (
                f"{winner} moves ahead {display} on serve midway through his "
                "opening game.",
                f"{winner} prend l'avantage, {_fr_spoken_score(current)}, "
                "au milieu de son premier jeu de service.",
                f"{winner} lidera por {_pt_spoken_score(current)} no meio do "
                "seu primeiro game de saque.",
            )
        return (
            f"{winner} moves ahead {display} on serve.",
            f"{winner} prend l'avantage, {_fr_spoken_score(current)}, "
            "sur son service.",
            f"{winner} lidera por {_pt_spoken_score(current)} no saque.",
        )
    if code == "receiver_ahead":
        return (
            f"{winner} moves ahead {display} on {server}'s serve.",
            f"{winner} prend l'avantage {_numeric_score_display(current)} "
            f"sur le service de {server}.",
            f"{winner} passa à frente por "
            f"{_numeric_score_display(current).replace('-', ' a ')} "
            f"no saque de {server}.",
        )
    if code == "set_complete":
        return (
            f"{winner} takes the set; {next_server} serves next.",
            f"Set {winner} ; {next_server} sera au service.",
            f"Set de {winner}; {next_server} saca em seguida.",
        )
    raise ValueError(f"unsupported score intent: {code}")


def score_commentary(
    previous: Score,
    current: Score,
    point_winners: list[str] | None = None,
) -> tuple[str, str, str]:
    """Describe an accepted transition, falling back only for legacy callers."""
    winner = point_winner(previous, current)
    if transition_type(previous, current) in {"point", "game", "set_reset"}:
        winners = list(point_winners or ([] if winner is None else [winner]))
        return render_score_intent(build_score_intent(previous, current, winners))
    return _ordinary_score_lines(current)


def score_call(previous: Score, current: Score) -> str:
    return score_commentary(previous, current)[0]


def score_localizations(previous: Score, current: Score) -> tuple[str, str]:
    _en, fr, pt = score_commentary(previous, current)
    return fr, pt


def rally_call(
    observation: str,
    index: int,
    score: Score | None = None,
) -> tuple[str, str, str] | None:
    """Fail closed on live-ball prose after v3 human review."""
    return None


def fast_score_sources(rows: list[dict]) -> list[dict]:
    """Convert corroborated local score events into normal detector sources."""
    sources = []
    for row in rows:
        score = row["score"]
        sources.append(
            {
                "video_time_s": float(row["video_time_s"]),
                "latency_s": float(row["latency_s"]),
                "corroborated": True,
                "observer": row["observer"],
                "detection": {
                    "phase": "between_points",
                    "live_play_confidence": 0.0,
                    "observation": (
                        "Local fixed-layout scoreboard observer confirmed a "
                        "legal score change in two consecutive frames."
                    ),
                    "scoreboard": {
                        "visible": True,
                        "far_sets": score["far_sets"],
                        "far_games": score["far_games"],
                        "far_points": score["far_points"],
                        "near_sets": score["near_sets"],
                        "near_games": score["near_games"],
                        "near_points": score["near_points"],
                        "server": score["server"],
                        "confidence": 0.99,
                    },
                },
            }
        )
    return sources


def _non_score_intent(
    *,
    kind: str,
    tracker: Score,
    reason: str,
    evidence: list[str],
    priority: str,
) -> dict:
    return {
        "type": kind,
        "code": kind,
        "transition": None,
        "winner": None,
        "server": tracker.server,
        "next_server": tracker.server,
        "point_number_in_game": None,
        "winner_streak": 0,
        "score_before": tracker.json(),
        "score_after": tracker.json(),
        "pressure_before": pressure_state(tracker, BEST_OF_SETS),
        "pressure_after": pressure_state(tracker, BEST_OF_SETS),
        "state_phase": kind,
        "evidence": evidence,
        "policy": {
            "decision": "emit",
            "reason": reason,
            "priority": priority,
        },
    }


def background_allowed(score: Score) -> tuple[bool, str]:
    pressure = pressure_state(score, BEST_OF_SETS)
    if not pressure["supported"]:
        return False, "unsupported_score_state"
    if pressure["kind"] == "deuce":
        return False, "deuce_requires_match_context"
    if pressure["kind"] != "game_point":
        return True, "ordinary_between_points_window"
    if pressure["match_point"]:
        return False, "match_point_requires_match_context"
    if pressure["set_point"]:
        return False, "set_point_requires_match_context"
    if pressure["break_point"]:
        return False, "break_point_requires_match_context"
    if int(pressure["count"]) <= 1:
        return False, "single_game_point_requires_match_context"
    return True, "multiple_server_game_points_leave_background_window"


def _row(
    *,
    source: dict,
    tracker: Score,
    attempt: int,
    src: str,
    text: str,
    fr: str,
    pt: str,
    stt: list[dict],
    intent: dict,
    changed: bool = False,
    vision: str | None = None,
    previous_tracker: Score | None = None,
) -> dict:
    t = float(source["video_time_s"])
    det = source["detection"]
    latency = float(source.get("latency_s", 0))
    return {
        "video_time_s": t,
        "src": src,
        "text": text,
        "fr": fr,
        "pt": pt,
        "vision": vision if vision is not None else det.get("observation", ""),
        "phase": det.get("phase"),
        "point": dict(NEUTRAL_POINT),
        "intent": intent,
        "policy": intent["policy"],
        "previous_tracker": (previous_tracker or tracker).json(),
        "tracker": tracker.json(),
        "tracker_changed": changed,
        "stt_context": _near_stt(stt, t),
        "attempt": attempt,
        "vision_latency_s": source.get("latency_s"),
        "commentary_latency_s": 0.0,
        "translation_latency_s": 0.0,
        "pipeline_latency_s": round(latency, 3),
        "dropped": latency > DELAY_S,
    }


def generate_attempt(
    detections: list[dict],
    stt: list[dict],
    attempt: int,
    fast_scores: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Schedule a TV-style middle ground from immutable accepted evidence."""
    fr, pt = FIXED_LOCALIZATIONS[INTRO]
    intro_intent = _non_score_intent(
        kind="pre_match",
        tracker=INITIAL,
        reason="opening_orientation_before_first_point",
        evidence=["verified_match_identity", "verified_initial_server"],
        priority="high",
    )
    rows = [
        {
            "video_time_s": 0.8,
            "src": "pre_match",
            "text": INTRO,
            "fr": fr,
            "pt": pt,
            "vision": "verified pre-match context",
            "phase": "pre_match",
            "point": dict(NEUTRAL_POINT),
            "intent": intro_intent,
            "policy": intro_intent["policy"],
            "previous_tracker": INITIAL.json(),
            "tracker": INITIAL.json(),
            "tracker_changed": False,
            "stt_context": [],
            "attempt": attempt,
            "vision_latency_s": 0.0,
            "commentary_latency_s": 0.0,
            "translation_latency_s": 0.0,
            "pipeline_latency_s": 0.0,
            "dropped": False,
        }
    ]
    tracker = ScoreTracker(INITIAL)
    tracker_rows: list[dict] = []
    last_candidate = 0.8
    color_index = 0
    changeovers: set[tuple[int, int, int, int]] = set()
    service_games: set[tuple[int, int, int, int, str]] = set()
    point_winners: list[str] = []

    def append(row: dict) -> None:
        nonlocal last_candidate
        rows.append(row)
        last_candidate = float(row["video_time_s"])

    sources = list(detections)
    if fast_scores:
        sources.extend(fast_score_sources(fast_scores))
    fast_score_times = [
        float(row["video_time_s"]) for row in (fast_scores or [])
    ]
    sources.sort(
        key=lambda item: (
            float(item["video_time_s"]),
            not item.get("corroborated", False),
        )
    )

    for source in sources:
        t = float(source["video_time_s"])
        det = source["detection"]
        previous_score = tracker.current
        score = _score_from_detection(det, previous_score)
        tracked = tracker.observe(
            score,
            float((det.get("scoreboard") or {}).get("confidence", 0)),
        )
        if (
            source.get("corroborated")
            and tracked.get("reason") == "awaiting_corroboration"
        ):
            tracked = tracker.observe(
                score,
                float((det.get("scoreboard") or {}).get("confidence", 0)),
            )
        tracker_rows.append(
            {
                "video_time_s": t,
                "text": score_text(tracker.current),
                "detail": (
                    f"{tracked['reason']}; "
                    f"phase={_state_phase(tracker.current, len(point_winners), 'point')}; "
                    f"stakes={pressure_state(tracker.current, BEST_OF_SETS)['kind']}"
                ),
                "accepted": tracked["accepted"],
                "changed": tracked.get("changed", False),
                "raw": score.json() if score else None,
                "observer": source.get("observer", "full_frame_vision"),
            }
        )
        phase = det.get("phase")
        source_ready = float(source.get("latency_s", DELAY_S + 1)) <= DELAY_S

        if tracked.get("changed"):
            winner = point_winner(previous_score, tracker.current)
            if winner:
                point_winners.append(winner)
            intent = build_score_intent(
                previous_score,
                tracker.current,
                point_winners,
            )
            if source.get("corroborated"):
                intent["evidence"].insert(
                    0, "local_fixed_layout_scoreboard_two_frame_confirmation"
                )
            tracker_rows[-1]["detail"] = (
                f"{tracked['reason']}; outcome={intent['code']}; "
                f"winner={intent['winner']}; phase={intent['state_phase']}; "
                f"stakes={intent['pressure_after']['kind']}"
            )
            text, fr, pt = render_score_intent(intent)
            append(
                _row(
                    source=source,
                    tracker=tracker.current,
                    attempt=attempt,
                    src="score_tracker",
                    text=text,
                    fr=fr,
                    pt=pt,
                    stt=stt,
                    intent=intent,
                    changed=True,
                    previous_tracker=previous_score,
                )
            )
            if intent["transition"] == "game":
                point_winners = []
            continue

        completed_games = tracker.current.far_games + tracker.current.near_games
        changeover_key = (
            tracker.current.far_sets,
            tracker.current.near_sets,
            tracker.current.far_games,
            tracker.current.near_games,
        )
        if (
            phase == "changeover"
            and completed_games == 1
            and changeover_key not in changeovers
            and t - last_candidate >= 6.0
            and source_ready
        ):
            fr, pt = FIXED_LOCALIZATIONS[CHANGEOVER_LINE]
            intent = _non_score_intent(
                kind="changeover",
                tracker=tracker.current,
                reason="first_game_complete_and_visual_changeover",
                evidence=["accepted_game_score", "vision_phase_changeover"],
                priority="medium",
            )
            append(
                _row(
                    source=source,
                    tracker=tracker.current,
                    attempt=attempt,
                    src="changeover",
                    text=CHANGEOVER_LINE,
                    fr=fr,
                    pt=pt,
                    stt=stt,
                    intent=intent,
                )
            )
            changeovers.add(changeover_key)
            continue

        service_key = (*changeover_key, tracker.current.server)
        if (
            phase == "serve_setup"
            and completed_games == 1
            and tracker.current.far_points == tracker.current.near_points == "0"
            and tracker.current.server == "far"
            and service_key not in service_games
            and float(det.get("live_play_confidence", 0)) >= 0.75
            and t - last_candidate >= 12.0
            and source_ready
        ):
            fr, pt = FIXED_LOCALIZATIONS[SERVICE_CONTEXT_LINE]
            intent = _non_score_intent(
                kind="serve_context",
                tracker=tracker.current,
                reason="new_server_setup_after_changeover",
                evidence=["accepted_server", "vision_serve_setup", "verified_handedness"],
                priority="medium",
            )
            append(
                _row(
                    source=source,
                    tracker=tracker.current,
                    attempt=attempt,
                    src="serve_context",
                    text=SERVICE_CONTEXT_LINE,
                    fr=fr,
                    pt=pt,
                    stt=stt,
                    intent=intent,
                )
            )
            service_games.add(service_key)
            continue

        if (
            phase in {"between_points", "changeover"}
            and color_index < len(COLOR_LINES)
            and t - last_candidate >= 18.0
            and source_ready
            and not any(0 < score_time - t < 8.0 for score_time in fast_score_times)
        ):
            allowed, policy_reason = background_allowed(tracker.current)
            if not allowed:
                continue
            text = COLOR_LINES[color_index]
            fr, pt = FIXED_LOCALIZATIONS[text]
            intent = _non_score_intent(
                kind="background",
                tracker=tracker.current,
                reason=policy_reason,
                evidence=["verified_pre_match_source", "between_points_window"],
                priority="low",
            )
            intent["evidence"].append("no_confirmed_score_change_within_8s")
            append(
                _row(
                    source=source,
                    tracker=tracker.current,
                    attempt=attempt,
                    src="pre_match_color",
                    text=text,
                    fr=fr,
                    pt=pt,
                    stt=stt,
                    intent=intent,
                    vision="verified pre-match context",
                )
            )
            color_index += 1

    return rows, tracker_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", type=int, required=True, choices=(1, 2, 3))
    args = parser.parse_args()
    assert_football_idle()
    detections = read_jsonl(SHARED_ARTIFACTS / "detections.jsonl")
    stt_path = SHARED_ARTIFACTS / "stt_merged.jsonl"
    stt = read_jsonl(
        stt_path if stt_path.exists() else SHARED_ARTIFACTS / "stt.jsonl"
    )
    if not detections:
        raise SystemExit("no detections; run detect.py first")

    fast_scores = read_jsonl(OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl")
    if not fast_scores:
        raise SystemExit("missing fast scoreboard artifact; run fast_scoreboard.py")
    rows, tracker_rows = generate_attempt(
        detections,
        stt,
        args.attempt,
        fast_scores=fast_scores,
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / f"commentary_attempt_{args.attempt}.jsonl"
    tracker_out = ARTIFACTS / "tracker.jsonl"
    out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )
    tracker_out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tracker_rows)
    )
    for row in rows:
        print(
            f"[{float(row['video_time_s']):6.1f}s] "
            f"{'DROP' if row['dropped'] else 'KEEP'} {row['text']}"
        )
    print(f"wrote {out} ({len(rows)} candidates)")


if __name__ == "__main__":
    main()

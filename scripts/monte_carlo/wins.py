"""Win checks and Mobster promotion (aligned with game.py)."""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Set

from config import WITCH_TOWN_LOSES_OUTCOMES
from guardian_angel_wins import bind_achieved_win, guardian_angel_joint_win
from stalemate_wins import NEUTRAL_KILLING_ROLES, lookup_two_player_stalemate
from scripts.monte_carlo.config import ARSO_HARMLESS_NEUTRALS, MAFIA, TOWN
from scripts.monte_carlo.state import Player, check_main_winner, _alive_ids


def try_two_player_stalemate_end(
    players: List[Player],
    alive: Set[int],
    out: Dict[str, bool],
) -> bool:
    """Apply ToS1 two-player stalemate table; return True if the sim should stop."""
    if len(alive) != 2:
        return False
    ids = sorted(alive)
    stalemate = lookup_two_player_stalemate(players[ids[0]].role, players[ids[1]].role)
    if not stalemate.applies or stalemate.winner is None:
        return False
    out[stalemate.winner] = True
    return True


def maybe_promote_mobster(
    players: List[Player],
    alive: Set[int],
    *,
    trace: bool,
    log: list[str],
) -> None:
    """game.check_win_conditions: promote random Mafia if none are Mobster."""
    mafia_alive = [pid for pid in alive if players[pid].role in MAFIA]
    if mafia_alive and not any(players[pid].role == "Mobster" for pid in mafia_alive):
        new_m = random.choice(mafia_alive)
        players[new_m].role = "Mobster"
        if trace:
            log.append(f"Mafia promotion: P{new_m} is promoted to Mobster.")


def arsonist_stalemate_win(players: List[Player], alive: Set[int]) -> bool:
    """True if Arsonist should win per game.py harmless-neutrals-only stalemate."""
    from faction_win_logic import arsonist_harmless_neutral_stalemate

    alive_ids = _alive_ids(players)
    roles = [players[pid].role for pid in alive_ids]
    if len(alive_ids) == 1:
        return roles[0] == "Arsonist"
    return arsonist_harmless_neutral_stalemate(roles)


def apply_personal_win_flags(
    players: List[Player],
    out: Dict[str, bool],
) -> None:
    """End-of-game personal wins (game.py stats / announce_survivor_style parity)."""
    alive_ids = {p.i for p in players if p is not None and p.alive}

    if not out.get("Draw"):
        for p in players:
            if p.role == "Survivor" and p.i in alive_ids:
                out["Survivor"] = True
                break
        for p in players:
            if p.role == "Chaos" and p.i in alive_ids:
                out["Chaos"] = True
                break

    for p in players:
        if p.role == "Pirate" and p.pirate_wins >= 2:
            out["Pirate"] = True
            break

    if len(alive_ids) == 1:
        only = next(iter(alive_ids))
        if players[only].role == "Serial Killer":
            out["Serial Killer"] = True

    main_outcome = None
    if out.get("Town"):
        main_outcome = "Town"
    elif out.get("Mafia"):
        main_outcome = "Mafia"
    elif out.get("Arsonist"):
        main_outcome = "Arsonist"
    elif out.get("Serial Killer"):
        main_outcome = "Serial Killer"

    if main_outcome in WITCH_TOWN_LOSES_OUTCOMES:
        for p in players:
            if p.role == "Witch" and p.i in alive_ids:
                out["Witch"] = True
                break

    if not out.get("Arsonist"):
        arso_alive = [pid for pid in alive_ids if players[pid].role == "Arsonist"]
        if arso_alive:
            arso = arso_alive[0]
            others = [pid for pid in alive_ids if pid != arso]
            if not others or all(
                players[pid].role in ARSO_HARMLESS_NEUTRALS or players[pid].role == "Arsonist"
                for pid in others
            ):
                out["Arsonist"] = True

    for p in players:
        if p.role != "Guardian Angel" or p.ga_bind_id is None:
            continue
        bind = players[p.ga_bind_id]
        if guardian_angel_joint_win(
            ga_alive=p.alive,
            ga_defeated=p.ga_defeated,
            bind_id=p.ga_bind_id,
            bind_role=bind.role,
            living_ids=alive_ids,
            outcome_flags=out,
            bind_pirate_wins=bind.pirate_wins,
            bind_exe_won=bind.exe_won if bind.role == "Executioner" else None,
            bind_jester_won=bind.jester_won if bind.role == "Jester" else None,
        ):
            out["Guardian Angel"] = True
            break

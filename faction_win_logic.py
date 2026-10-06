"""Shared Town/Mafia/Arsonist stalemate rules for live game and Monte Carlo."""
from __future__ import annotations

from typing import Iterable, List, Optional

from config import ALL_MAFIA_ROLES, TOWN_ROLES
from faction_taxonomy import arsonist_harmless_neutral_roles, count_town_roles
from stalemate_wins import NEUTRAL_KILLING_ROLES, lookup_two_player_stalemate

# game.py check_win_conditions + MC arsonist_stalemate_win
ARSONIST_HARMLESS_NEUTRALS = arsonist_harmless_neutral_roles()


def living_roles_faction_winner(living_roles: Iterable[str]) -> Optional[str]:
    """
    Return ``Town`` or ``Mafia`` when a faction win applies.

    Ignores Arsonist / SK solo and two-player stalemate table (handled elsewhere).
    Returns None if the game should continue.
    """
    roles = list(living_roles)
    if not roles:
        return None
    mafia_count = sum(1 for r in roles if r in ALL_MAFIA_ROLES)
    town_count = count_town_roles(roles)
    arso_alive = "Arsonist" in roles
    killing_neutral_alive = any(r in NEUTRAL_KILLING_ROLES for r in roles)
    if mafia_count == 0 and town_count > 0 and not arso_alive and not killing_neutral_alive:
        return "Town"
    non_mafia_count = len(roles) - mafia_count
    if mafia_count > 0 and mafia_count >= non_mafia_count and not arso_alive:
        return "Mafia"
    return None


def arsonist_harmless_neutral_stalemate(living_roles: Iterable[str]) -> bool:
    """True when only Arsonist + harmless neutrals remain (no Town/Mafia)."""
    roles = list(living_roles)
    if "Arsonist" not in roles or len(roles) < 2:
        return False
    others = [r for r in roles if r != "Arsonist"]
    if not others:
        return True
    if any(r in ALL_MAFIA_ROLES for r in others):
        return False
    if any(r in TOWN_ROLES for r in others):
        return False
    return all(r in ARSONIST_HARMLESS_NEUTRALS for r in others)

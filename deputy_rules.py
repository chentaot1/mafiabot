"""Deputy daytime revolver rules (shared by bot commands and MC/sim bridge)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import ALL_MAFIA_ROLES
from engine import combat as combat_tiers
from engine.night import tamper_subject_for_submitted_slot
from faction_taxonomy import deputy_gun_evil_neutral_roles

if TYPE_CHECKING:
    from game import Game


def deputy_gun_sees_evil(game: "Game", pid: int) -> bool:
    subject = tamper_subject_for_submitted_slot(game, pid)
    if game.player_roles.get(subject) in ALL_MAFIA_ROLES:
        return True
    if game.player_roles.get(subject) in deputy_gun_evil_neutral_roles():
        return True
    if game.role_states.get(subject, {}).get("is_framed"):
        return True
    try:
        return int(subject) in game.doused_players
    except (TypeError, ValueError):
        return False


def deputy_shot_blocked_by_defense(game: "Game", pid: int) -> bool:
    """True when the Deputy's unstoppable daytime shot fails to pierce passive/vest defense."""
    return not combat_tiers.deputy_shot_would_kill_target(game, pid)


# Back-compat aliases for monte_carlo bridge / night_cmds imports.
_deputy_gun_sees_evil = deputy_gun_sees_evil
_deputy_target_basic_defense = deputy_shot_blocked_by_defense

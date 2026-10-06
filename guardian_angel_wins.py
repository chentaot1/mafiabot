"""Guardian Angel joint-win rules (bot endgame + Monte Carlo).

GA personal win (``guardian_angel_win`` stat):
  - Joint: GA alive, bind alive, bind achieved their win at endgame.
  - Stalemate override: GA alive, not defeated, bind alive (ToS1 draw override).
"""
from __future__ import annotations

from typing import AbstractSet, Any, Dict, Mapping, Optional

from config import ALL_MAFIA_ROLES, TOWN_ROLES, WITCH_TOWN_LOSES_OUTCOMES


def bind_achieved_win(
    bind_role: str,
    bind_id: int,
    *,
    living_ids: AbstractSet[int],
    outcome_flags: Mapping[str, bool],
    bind_role_state: Optional[Mapping[str, Any]] = None,
    bind_pirate_wins: Optional[int] = None,
    bind_exe_won: Optional[bool] = None,
    bind_jester_won: Optional[bool] = None,
) -> bool:
    """True when this bind seat fulfilled their win condition at endgame."""
    if int(bind_id) not in {int(x) for x in living_ids}:
        return False

    if bind_role in TOWN_ROLES:
        return bool(outcome_flags.get("Town"))

    if bind_role in ALL_MAFIA_ROLES:
        return bool(outcome_flags.get("Mafia"))

    # Neutral benign on another GA: no solo path — require Town win while bind lives.
    if bind_role == "Guardian Angel":
        return bool(outcome_flags.get("Town"))

    st = bind_role_state or {}

    if bind_role == "Survivor":
        return bool(outcome_flags.get("Survivor"))
    if bind_role == "Chaos":
        return bool(outcome_flags.get("Chaos"))
    if bind_role == "Witch":
        return bool(outcome_flags.get("Witch"))
    if bind_role == "Pirate":
        if bind_pirate_wins is not None:
            return int(bind_pirate_wins) >= 2
        return bool(outcome_flags.get("Pirate"))
    if bind_role == "Jester":
        if bind_jester_won is not None:
            return bool(bind_jester_won)
        return bool(outcome_flags.get("Jester"))
    if bind_role == "Executioner":
        if bind_exe_won is not None:
            return bool(bind_exe_won)
        return bool(outcome_flags.get("Executioner"))
    if bind_role == "Arsonist":
        return bool(outcome_flags.get("Arsonist"))
    if bind_role == "Serial Killer":
        return bool(outcome_flags.get("Serial Killer"))

    return False


def guardian_angel_joint_win(
    *,
    ga_alive: bool,
    ga_defeated: bool,
    bind_id: Optional[int],
    bind_role: Optional[str],
    living_ids: AbstractSet[int],
    outcome_flags: Mapping[str, bool],
    bind_role_state: Optional[Mapping[str, Any]] = None,
    bind_pirate_wins: Optional[int] = None,
    bind_exe_won: Optional[bool] = None,
    bind_jester_won: Optional[bool] = None,
) -> bool:
    if not ga_alive or ga_defeated or bind_id is None or not bind_role:
        return False
    return bind_achieved_win(
        bind_role,
        int(bind_id),
        living_ids=living_ids,
        outcome_flags=outcome_flags,
        bind_role_state=bind_role_state,
        bind_pirate_wins=bind_pirate_wins,
        bind_exe_won=bind_exe_won,
        bind_jester_won=bind_jester_won,
    )


def guardian_angel_personal_win(
    *,
    role: str,
    player_id: int,
    ga_alive: bool,
    ga_defeated: bool,
    ga_joint_win: bool,
    stalemate_override: bool,
    override_winner_ids: AbstractSet[int],
) -> bool:
    """Single GA personal stat: joint bind-achieved win OR stalemate override."""
    if role != "Guardian Angel":
        return False
    if ga_joint_win:
        return True
    if (
        stalemate_override
        and ga_alive
        and not ga_defeated
        and int(player_id) in {int(x) for x in override_winner_ids}
    ):
        return True
    return False


def outcome_flags_from_norm(outcome_norm: str) -> Dict[str, bool]:
    return {
        "Town": outcome_norm == "Town",
        "Mafia": outcome_norm == "Mafia",
        "Arsonist": outcome_norm == "Arsonist",
        "Serial Killer": outcome_norm == "Serial Killer",
        "Draw": outcome_norm == "Draw",
    }


def build_outcome_flags_for_game(
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
    outcome_norm: str,
) -> Dict[str, bool]:
    """Endgame outcome flags for GA joint-win (mirrors MC ``out`` after personal wins)."""
    flags = outcome_flags_from_norm(outcome_norm)
    if outcome_norm == "Draw":
        return flags
    for pid in living_ids:
        role = player_roles.get(int(pid))
        if not role:
            continue
        personal = bind_personal_flags_for_game(
            int(pid),
            str(role),
            living_ids=living_ids,
            outcome_norm=outcome_norm,
            bind_role_state=role_states.get(int(pid), {}) or {},
        )
        for key, val in personal.items():
            if val:
                flags[key] = True
    return flags


def bind_personal_flags_for_game(
    bind_id: int,
    bind_role: str,
    *,
    living_ids: AbstractSet[int],
    outcome_norm: str,
    bind_role_state: Mapping[str, Any],
) -> Dict[str, bool]:
    """Personal-win slice of ``outcome_flags`` for one bind seat (game.py)."""
    alive = int(bind_id) in {int(x) for x in living_ids}
    flags = outcome_flags_from_norm(outcome_norm)
    if outcome_norm == "Draw" or not alive:
        return flags

    if bind_role == "Survivor":
        flags["Survivor"] = True
    elif bind_role == "Chaos":
        flags["Chaos"] = True
    elif bind_role == "Witch" and outcome_norm in WITCH_TOWN_LOSES_OUTCOMES:
        flags["Witch"] = True
    elif bind_role == "Pirate" and int(bind_role_state.get("wins", 0)) >= 2:
        flags["Pirate"] = True
    elif bind_role == "Jester" and bool(bind_role_state.get("jester_won")):
        flags["Jester"] = True
    elif bind_role == "Executioner" and bool(bind_role_state.get("exe_won")):
        flags["Executioner"] = True
    elif bind_role == "Arsonist" and outcome_norm == "Arsonist":
        flags["Arsonist"] = True
    elif bind_role == "Serial Killer" and outcome_norm == "Serial Killer":
        flags["Serial Killer"] = True

    return flags

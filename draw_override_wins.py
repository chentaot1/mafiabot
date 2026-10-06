"""ToS1 stalemate draw override — personal neutrals replace Draw when they succeeded."""

from __future__ import annotations

from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Tuple

# Roles that can replace a stalemate Draw with a solo / personal victory screen.
# Pirate and Chaos are Neutral Chaos in this bot (Chaos: survivorship like Survivor).
# Pirate override requires 2 plunders; Chaos override requires survival.
DRAW_OVERRIDE_ROLES: Tuple[str, ...] = (
    "Survivor",
    "Chaos",
    "Jester",
    "Executioner",
    "Pirate",
    "Guardian Angel",
)

DRAW_OVERRIDE_OUTCOMES = frozenset(DRAW_OVERRIDE_ROLES)

# Stored game outcome when multiple override winners (stats / SQLite).
DRAW_OVERRIDE_MULTI_OUTCOME = "Neutral Personal"

# Priority for a single stored outcome when several roles override Draw.
_OUTCOME_PRIORITY: Tuple[str, ...] = DRAW_OVERRIDE_ROLES


def is_draw_override_outcome(outcome_norm: str) -> bool:
    return str(outcome_norm) in DRAW_OVERRIDE_OUTCOMES or str(outcome_norm) == DRAW_OVERRIDE_MULTI_OUTCOME


def guardian_angel_draw_override_win(
    *,
    ga_alive: bool,
    ga_defeated: bool,
    bind_id: Optional[int],
    living_ids: AbstractSet[int],
) -> bool:
    """ToS1 stalemate override: GA kept their bind alive (bind need not have 'won' otherwise)."""
    if not ga_alive or ga_defeated or bind_id is None:
        return False
    return int(bind_id) in {int(x) for x in living_ids}


def player_counts_as_loss_on_draw(*, role: str, did_win: bool, is_draw: bool) -> bool:
    """
    ToS1 draw scoring: most roles tie (no win/loss); Witch is the exception and loses.
    """
    if did_win:
        return False
    if not is_draw:
        return True
    return role == "Witch"


def build_draw_override_outcome_flags(
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
) -> Dict[str, bool]:
    """Outcome flags for GA joint-win and stats when Draw was overridden."""
    living_set = {int(x) for x in living_ids}
    flags: Dict[str, bool] = {
        "Town": False,
        "Mafia": False,
        "Arsonist": False,
        "Serial Killer": False,
        "Draw": False,
        "Survivor": False,
        "Jester": False,
        "Executioner": False,
        "Pirate": False,
        "Guardian Angel": False,
        "Chaos": False,
        "Witch": False,
    }
    for pid, role in player_roles.items():
        st = role_states.get(int(pid), {}) or {}
        ipid = int(pid)
        if role == "Survivor" and ipid in living_set:
            flags["Survivor"] = True
        elif role == "Chaos" and ipid in living_set:
            flags["Chaos"] = True
        elif role == "Jester" and bool(st.get("jester_won")):
            flags["Jester"] = True
        elif role == "Executioner" and bool(st.get("exe_won")):
            flags["Executioner"] = True
        elif role == "Pirate" and int(st.get("wins", 0)) >= 2:
            flags["Pirate"] = True
    for pid, role in player_roles.items():
        if role != "Guardian Angel":
            continue
        ipid = int(pid)
        st = role_states.get(ipid, {}) or {}
        bind_raw = st.get("ga_target_id")
        try:
            bind_id = int(bind_raw) if bind_raw is not None else None
        except (TypeError, ValueError):
            bind_id = None
        if guardian_angel_draw_override_win(
            ga_alive=ipid in living_set,
            ga_defeated=bool(st.get("ga_defeated")),
            bind_id=bind_id,
            living_ids=living_set,
        ):
            flags["Guardian Angel"] = True
    return flags


def collect_draw_override_winners(
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
) -> List[Tuple[int, str]]:
    """
    Return (player_id, role) for every seat that claims a stalemate Draw override.

    Order follows ``_OUTCOME_PRIORITY`` (stable, ToS-style presentation order).
    """
    living_set = {int(x) for x in living_ids}
    by_role: Dict[str, List[int]] = {r: [] for r in DRAW_OVERRIDE_ROLES}

    for pid, role in player_roles.items():
        ipid = int(pid)
        st = role_states.get(ipid, {}) or {}
        if role == "Survivor" and ipid in living_set:
            by_role["Survivor"].append(ipid)
        elif role == "Chaos" and ipid in living_set:
            by_role["Chaos"].append(ipid)
        elif role == "Jester" and bool(st.get("jester_won")):
            by_role["Jester"].append(ipid)
        elif role == "Executioner" and bool(st.get("exe_won")):
            by_role["Executioner"].append(ipid)
        elif role == "Pirate" and int(st.get("wins", 0)) >= 2:
            by_role["Pirate"].append(ipid)

    for pid, role in player_roles.items():
        if role != "Guardian Angel":
            continue
        ipid = int(pid)
        st = role_states.get(ipid, {}) or {}
        bind_raw = st.get("ga_target_id")
        try:
            bind_id = int(bind_raw) if bind_raw is not None else None
        except (TypeError, ValueError):
            bind_id = None
        if guardian_angel_draw_override_win(
            ga_alive=ipid in living_set,
            ga_defeated=bool(st.get("ga_defeated")),
            bind_id=bind_id,
            living_ids=living_set,
        ):
            by_role["Guardian Angel"].append(ipid)

    winners: List[Tuple[int, str]] = []
    seen: set[int] = set()
    for role in _OUTCOME_PRIORITY:
        for pid in sorted(by_role.get(role, [])):
            if pid in seen:
                continue
            seen.add(pid)
            winners.append((pid, role))
    return winners


def resolve_draw_override_outcome(winners: List[Tuple[int, str]]) -> str:
    """SQLite / stats outcome string for an overridden stalemate."""
    if not winners:
        return "Draw"
    roles = sorted({r for _, r in winners})
    if len(roles) == 1:
        return roles[0]
    return f"{DRAW_OVERRIDE_MULTI_OUTCOME} ({', '.join(roles)})"


def draw_override_winner_ids(
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
) -> frozenset[int]:
    return frozenset(pid for pid, _ in collect_draw_override_winners(player_roles, role_states, living_ids))


def role_states_from_mc_players(players: Any) -> Dict[int, Dict[str, Any]]:
    """Build game.py-shaped role_states from Monte Carlo ``Player`` objects."""
    out: Dict[int, Dict[str, Any]] = {}
    for p in players:
        st: Dict[str, Any] = {}
        if int(getattr(p, "pirate_wins", 0)) > 0:
            st["wins"] = int(p.pirate_wins)
        if bool(getattr(p, "exe_won", False)):
            st["exe_won"] = True
        if bool(getattr(p, "jester_won", False)):
            st["jester_won"] = True
        if getattr(p, "ga_bind_id", None) is not None:
            st["ga_target_id"] = p.ga_bind_id
        if bool(getattr(p, "ga_defeated", False)):
            st["ga_defeated"] = True
        out[int(p.i)] = st
    return out


def apply_stalemate_draw_override(
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
    out: Dict[str, bool],
) -> Optional[str]:
    """
    If a ToS1 draw override applies, set ``out`` personal-win flags and return the outcome string.
    Returns None when a true Draw should be recorded.
    """
    winners = collect_draw_override_winners(player_roles, role_states, living_ids)
    if not winners:
        return None
    out["Draw"] = False
    for _pid, role in winners:
        out[role] = True
    return resolve_draw_override_outcome(winners)

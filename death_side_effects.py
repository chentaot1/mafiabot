"""Shared death bookkeeping used by process_death and process_death_by_id."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game


def record_death_metadata(game: "Game", player_id: int, *, cause: str) -> None:
    """Persist first-write death cause/day on role_states."""
    ds = game.role_states.setdefault(int(player_id), {})
    ds.setdefault("death_cause", str(cause))
    ds.setdefault("died_day", int(game.day_number))


def append_graveyard_entry(
    game: "Game",
    player_id: int,
    *,
    real_role: str,
    cause: str,
    is_hidden: bool,
) -> None:
    game.graveyard.append(
        {
            "player_id": int(player_id),
            "real_role": real_role,
            "died_day": int(game.day_number),
            "cause": cause,
            "is_hidden": bool(is_hidden),
            "used_by_retri": False,
        }
    )


def apply_ga_defeat_on_bind_death(
    game: "Game",
    deceased_id: int,
    *,
    cause: str,
) -> None:
    """Guardian Angel defeated when bind dies (trial-lock lynch exception)."""
    for ga_id, ga_st in list(game.role_states.items()):
        if game.player_roles.get(ga_id) != "Guardian Angel":
            continue
        bind_raw = ga_st.get("ga_target_id")
        try:
            bid = int(bind_raw) if bind_raw is not None else None
        except (TypeError, ValueError):
            bid = None
        if bid is None or int(deceased_id) != bid:
            continue
        if str(cause) == "lynch":
            lock_day = game.role_states.get(bid, {}).get("ga_trial_lock_day")
            try:
                lock_ok = lock_day is not None and int(game.day_number) == int(lock_day)
            except (TypeError, ValueError):
                lock_ok = False
            if lock_ok:
                continue
        ga_st["ga_defeated"] = True


def apply_jester_lynch_flags(game: "Game", player_id: int, *, real_role: str, cause: str) -> None:
    if real_role == "Jester" and cause == "lynch":
        s = game.role_states.setdefault(int(player_id), {})
        s["can_haunt"] = True
        s["jester_won"] = True


def apply_core_death_bookkeeping(
    game: "Game",
    player_id: int,
    *,
    real_role: str,
    cause: str,
    is_hidden: bool,
    record_cycle: bool = True,
) -> None:
    """Shared graveyard + metadata + GA/Jester flags (both death entry points)."""
    if record_cycle:
        game.record_cycle_death(1)
    record_death_metadata(game, player_id, cause=cause)
    append_graveyard_entry(
        game,
        player_id,
        real_role=real_role,
        cause=cause,
        is_hidden=is_hidden,
    )
    apply_ga_defeat_on_bind_death(game, player_id, cause=cause)
    apply_jester_lynch_flags(game, player_id, real_role=real_role, cause=cause)

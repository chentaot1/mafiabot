"""Death side-effect parity for Monte Carlo (mirrors death_side_effects.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from scripts.monte_carlo.state import Player


def apply_ga_bind_death_sim(
    players: List["Player"],
    deceased_id: int,
    *,
    cause: str,
    day: int,
) -> None:
    """Guardian Angel defeated when bind dies (trial-lock lynch exception)."""
    for ga in players:
        if ga.role != "Guardian Angel" or ga.ga_bind_id is None:
            continue
        if int(ga.ga_bind_id) != int(deceased_id):
            continue
        if str(cause) == "lynch":
            bind = players[int(ga.ga_bind_id)]
            lock_day = getattr(bind, "ga_trial_lock_day", None)
            try:
                lock_ok = lock_day is not None and int(day) == int(lock_day)
            except (TypeError, ValueError):
                lock_ok = False
            if lock_ok:
                continue
        ga.ga_defeated = True

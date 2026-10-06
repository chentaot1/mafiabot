"""Retributionist use/corpse consumption after night engine (live + MC)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Collection, Dict, Mapping, Union

if TYPE_CHECKING:
    from game import Game

HealedByMap = Mapping[int, int]


def consume_retributionist_uses(
    game: "Game",
    blocked: Collection[int],
    healed_by: Union[HealedByMap, Dict[int, int]],
) -> None:
    """
    Decrement Retributionist uses for each submitted corpse action (``_from_retri``).

    Consumption does not depend on whether heal, shoot, roleblock, etc. succeeded.
    Mirrors live ``!resolve`` and Monte Carlo ``resolve_night_via_engine``.

    ``expand_reanimate_actions`` runs here so crash-resume post-pipeline paths
    (which skip the main resolve expand step) still see ``_from_retri``.
    """
    del blocked, healed_by

    from reanimate_expand import expand_reanimate_actions

    expand_reanimate_actions(game)

    for actor_id, action in list(game.night_actions.items()):
        if game.player_roles.get(actor_id) != "Retributionist":
            continue

        corpse_pid = action.get("_from_retri")
        if corpse_pid is None:
            continue

        try:
            corpse_int = int(corpse_pid)
        except (TypeError, ValueError):
            continue

        from reanimate_expand import mark_retributionist_corpse_used

        s = game.role_states.setdefault(int(actor_id), {})
        used = s.setdefault("used_corpses", [])
        try:
            used_ints = {int(x) for x in used}
        except (TypeError, ValueError):
            used_ints = set()
        if corpse_int in used_ints:
            continue

        from night_action_eligibility import retributionist_consume_eligible

        if not retributionist_consume_eligible(game, int(actor_id)):
            continue

        from persist_schema import coerce_role_state_int

        uses = coerce_role_state_int(s.get("uses_remaining"), 0)
        s["uses_remaining"] = max(0, uses - 1)
        mark_retributionist_corpse_used(
            game, retri_player_id=int(actor_id), corpse_player_id=corpse_int
        )

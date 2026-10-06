"""Shared night-resolve prep (expand reanimate) for live ``gm`` and MC bridge."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from game import Game


def expand_reanimate_for_night_resolve(game: "Game") -> List[Any]:
    """Single entry for Retributionist corpse expansion before the night engine."""
    from reanimate_expand import expand_reanimate_actions

    return expand_reanimate_actions(game)


async def notify_reanimate_expand_failures(
    game: "Game",
    guild: object,
    failed: List[Any],
) -> None:
    """Notify Retributionists when expansion fails (no-op for headless MC guilds)."""
    if not failed:
        return
    if getattr(guild, "_monte_carlo_fake", False):
        return
    from reanimate_expand import notify_retributionist_expand_failures

    await notify_retributionist_expand_failures(game, guild, failed)  # type: ignore[arg-type]

"""Single source of truth for role faction buckets (guilt, wins, visions, stats)."""
from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, Iterable

from config import (
    ALL_MAFIA_ROLES,
    DEPUTY_GUN_EVIL_NEUTRALS,
    NEUTRAL_BENIGN_ROLES,
    PSYCHIC_ODD_EVIL_NEUTRALS,
    SEER_FRIENDLY_EXTRA_ROLES,
    SEER_HOSTILE_NEUTRAL_ROLES,
    SEER_NEUTRAL_KILLING_ROLES,
    TOWN_ROLES,
)

# Re-export config lists for callers that need the raw sets.
__all__ = [
    "ALL_MAFIA_ROLES",
    "NEUTRAL_BENIGN_ROLES",
    "PSYCHIC_ODD_EVIL_NEUTRALS",
    "SEER_FRIENDLY_EXTRA_ROLES",
    "SEER_HOSTILE_NEUTRAL_ROLES",
    "SEER_NEUTRAL_KILLING_ROLES",
    "TOWN_ROLES",
    "arsonist_harmless_neutral_roles",
    "count_town_roles",
    "psychic_even_night_good_role",
    "role_endgame_faction_bucket",
    "seer_friends_roles",
    "triggers_vig_guilt_on_kill",
    "deputy_gun_evil_neutral_roles",
    "psychic_odd_evil_roles",
    "psychic_vision_living_too_small",
    "psychic_vision_pool_too_small_odd",
    "psychic_vision_pool_too_small_even",
]


@lru_cache(maxsize=1)
def _town_roles() -> FrozenSet[str]:
    return frozenset(TOWN_ROLES)


@lru_cache(maxsize=1)
def arsonist_harmless_neutral_roles() -> FrozenSet[str]:
    """Roles that do not block Arsonist stalemate when alive with only Arsonist (+ each other)."""
    return frozenset(
        {
            "Witch",
            "Survivor",
            "Executioner",
            "Jester",
            "Chaos",
            "Guardian Angel",
            "Pirate",
            "Psychic",
            "Deputy",
            "Seer",
        }
    )


def triggers_vig_guilt_on_kill(role: str) -> bool:
    """True when a Vig / Retri-Vig corpse kill that kills this role should apply guilt."""
    return role in _town_roles()


def psychic_even_night_good_role(role: str) -> bool:
    """Psychic even-night vision pool (Town bucket + Survivor)."""
    return role in _town_roles() or role == "Survivor"


@lru_cache(maxsize=1)
def deputy_gun_evil_neutral_roles() -> FrozenSet[str]:
    return frozenset(DEPUTY_GUN_EVIL_NEUTRALS)


@lru_cache(maxsize=1)
def psychic_odd_evil_roles() -> FrozenSet[str]:
    return frozenset(PSYCHIC_ODD_EVIL_NEUTRALS)


def psychic_vision_living_too_small(living_count: int) -> bool:
    """Engine: Psychic gets no vision when ``len(living_set) <= 3``."""
    return int(living_count) <= 3


def psychic_vision_pool_too_small_odd(pool_size: int) -> bool:
    """Engine: odd-night evil vision needs at least 3 candidates in pool."""
    return int(pool_size) < 3


def psychic_vision_pool_too_small_even(pool_size: int) -> bool:
    """Engine: even-night good vision needs at least 2 candidates in pool."""
    return int(pool_size) < 2


def seer_friends_roles() -> FrozenSet[str]:
    return _town_roles() | frozenset(SEER_FRIENDLY_EXTRA_ROLES)


def count_town_roles(roles: Iterable[str]) -> int:
    town = _town_roles()
    return sum(1 for r in roles if r in town)


def role_endgame_faction_bucket(role: str) -> str:
    if role in _town_roles():
        return "Town"
    if role in ALL_MAFIA_ROLES:
        return "Mafia"
    return "Neutral"

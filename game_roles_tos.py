"""ToS1-inspired category-slot role generation (Classic + RT duplicate experiments).

Used by prod ``!startgame`` at 5p–7p and by Monte Carlo manifests.
"""
from __future__ import annotations

import random
from typing import Dict, List, Sequence, Set, Tuple

from game_roles import (
    KILLING_NEUTRALS,
    LOBBY_UNIQUE_ROLES,
    NEUTRAL_BENIGN_POOL,
    NEUTRAL_BUCKET_ORDER,
    NEUTRAL_BUCKET_POOLS,
    NEUTRAL_BUCKET_WEIGHTS,
    NEUTRAL_CHAOS_POOL,
    NEUTRAL_CHAOS_ROLES,
    NEUTRAL_EVIL_POOL,
    NEUTRAL_EVIL_ROLES,
    NEUTRAL_KILLING_POOL,
    NEUTRAL_KILLING_ROLES,
    UNIQUE_NEUTRAL_ROLES,
    draw_distinct_neutral_buckets,
)

CategoryPool = List[Tuple[str, int]]

TOWN_INVESTIGATIVE: CategoryPool = [
    ("Sheriff", 8),
    ("Investigator", 6),
    ("Lookout", 7),
    ("Tracker", 7),
    ("Psychic", 5),
    ("Seer", 5),
]
TOWN_PROTECTIVE: CategoryPool = [
    ("Doctor", 8),
    ("Bodyguard", 6),
]
TOWN_SUPPORT: CategoryPool = [
    ("Escort", 7),
    ("Mayor", 3),
    ("Transporter", 4),
    ("Retributionist", 3),
]
TOWN_KILLING: CategoryPool = [
    ("Vigilante", 6),
    ("Deputy", 4),
    ("Scary Grandma", 5),
]

NEUTRAL_BENIGN: CategoryPool = NEUTRAL_BENIGN_POOL
NEUTRAL_EVIL: CategoryPool = NEUTRAL_EVIL_POOL
NEUTRAL_KILLING: CategoryPool = NEUTRAL_KILLING_POOL
NEUTRAL_CHAOS: CategoryPool = NEUTRAL_CHAOS_POOL
NEUTRAL_NON_BENIGN: CategoryPool = NEUTRAL_EVIL + NEUTRAL_KILLING + NEUTRAL_CHAOS

NeutralCategory = str
NEUTRAL_CATEGORY_WEIGHTS: Tuple[Tuple[NeutralCategory, int], ...] = tuple(
    (b, NEUTRAL_BUCKET_WEIGHTS[b]) for b in NEUTRAL_BUCKET_ORDER
)
_CASUAL_NEUTRAL_CATEGORIES: Tuple[NeutralCategory, ...] = NEUTRAL_BUCKET_ORDER

_NEUTRAL_CATEGORY_POOLS: Dict[NeutralCategory, CategoryPool] = NEUTRAL_BUCKET_POOLS

# Classic: every role unique once per game.
NON_UNIQUE_ROLES: Set[str] = set()

# Partial-unique mode: only globally unique roles cannot duplicate.
UNIQUE_ROLES_RT_DUPE: Set[str] = set(LOBBY_UNIQUE_ROLES) | set(UNIQUE_NEUTRAL_ROLES)

TOWN_RANDOM: CategoryPool = (
    TOWN_INVESTIGATIVE + TOWN_PROTECTIVE + TOWN_SUPPORT + TOWN_KILLING
)

TOS_CLASSIC_7P_TOWN_SLOTS: Tuple[str, ...] = (
    "town_investigative",
    "town_protective",
    "town_support",
    "town_killing",
)

_CATEGORY_POOLS: Dict[str, CategoryPool] = {
    "town_investigative": TOWN_INVESTIGATIVE,
    "town_protective": TOWN_PROTECTIVE,
    "town_support": TOWN_SUPPORT,
    "town_killing": TOWN_KILLING,
    "neutral_benign": NEUTRAL_BENIGN,
}

NEUTRAL_ALL: CategoryPool = NEUTRAL_BENIGN + NEUTRAL_NON_BENIGN


def _pick_weighted(
    pool: CategoryPool,
    *,
    chosen: Set[str],
    rng: random.Random,
    unique_roles: Set[str] | None = None,
    roles_so_far: Sequence[str] | None = None,
) -> str:
    """Pick one role from pool. Duplicates allowed unless role is in ``unique_roles``."""
    unique = unique_roles if unique_roles is not None else NON_UNIQUE_ROLES
    taken = list(roles_so_far) if roles_so_far is not None else list(chosen)
    available: List[Tuple[str, int]] = []
    for role, weight in pool:
        if role in unique and role in taken:
            continue
        available.append((role, weight))
    if not available:
        raise ValueError("Category pool exhausted (no available roles)")
    names = [r for r, _w in available]
    weights = [w for _r, w in available]
    return rng.choices(names, weights=weights, k=1)[0]


def _pick_neutral_second(
    roles_so_far: Sequence[str],
    rng: random.Random,
    *,
    unique_roles: Set[str],
) -> str:
    """NE / NK / NC slot with killing cap (mirrors draw_neutrals)."""
    killing = sum(1 for r in roles_so_far if r in KILLING_NEUTRALS)
    candidates: CategoryPool = []
    for role, weight in NEUTRAL_NON_BENIGN:
        if role in unique_roles and role in roles_so_far:
            continue
        if role in KILLING_NEUTRALS and killing >= 1:
            continue
        candidates.append((role, weight))
    if not candidates:
        raise ValueError("Neutral slot-2 exhausted under caps")
    names = [r for r, _w in candidates]
    weights = [w for _r, w in candidates]
    return rng.choices(names, weights=weights, k=1)[0]


def _pick_neutral_rt_dupe(roles_so_far: List[str], rng: random.Random) -> str:
    """Legacy: one neutral from full pool with NK cap."""
    killing = sum(1 for r in roles_so_far if r in KILLING_NEUTRALS)
    candidates: CategoryPool = []
    for role, weight in NEUTRAL_ALL:
        if role in UNIQUE_NEUTRAL and role in roles_so_far:
            continue
        if role in KILLING_NEUTRALS and killing >= 1:
            continue
        candidates.append((role, weight))
    if not candidates:
        raise ValueError("Neutral draw exhausted under caps")
    names = [r for r, _w in candidates]
    weights = [w for _r, w in candidates]
    return rng.choices(names, weights=weights, k=1)[0]


def _draw_casual_neutral_categories(rng: random.Random) -> Tuple[NeutralCategory, NeutralCategory]:
    """Two distinct buckets; 25% weight per bucket on each draw (slot marginals 25% each)."""
    weights = [NEUTRAL_BUCKET_WEIGHTS[c] for c in _CASUAL_NEUTRAL_CATEGORIES]
    cat_a = rng.choices(_CASUAL_NEUTRAL_CATEGORIES, weights=weights, k=1)[0]
    remaining = [c for c in _CASUAL_NEUTRAL_CATEGORIES if c != cat_a]
    rem_weights = [NEUTRAL_BUCKET_WEIGHTS[c] for c in remaining]
    cat_b = rng.choices(remaining, weights=rem_weights, k=1)[0]
    return cat_a, cat_b


def _append_casual_neutrals(
    roles: List[str],
    rng: random.Random,
    *,
    unique_roles: Set[str],
    num_neutral: int = 2,
    allowed_roles: Set[str] | None = None,
) -> None:
    """Append neutrals via prod bucket draw."""
    if num_neutral <= 0:
        return
    allowed = allowed_roles or {r for pool in NEUTRAL_BUCKET_POOLS.values() for r, _w in pool}
    picked = draw_distinct_neutral_buckets(
        num_neutral,
        allowed_roles=allowed,
        rng=rng,
        unique_roles=unique_roles,
    )
    roles.extend(picked)


_RT_DUPE_RT_SLOT_COUNT: Dict[int, int] = {6: 2, 7: 2}
_RT_DUPE_NEUTRAL_COUNT: Dict[int, int] = {6: 1, 7: 2}


def _neutral_allowed_roles(player_count: int) -> Set[str]:
    """6p+ full neutral bucket pools; 5p prod has no neutral slot."""
    if player_count == 5:
        return set()
    return {r for pool in NEUTRAL_BUCKET_POOLS.values() for r, _w in pool}


def sample_tos_classic_roles(player_count: int, *, rng: random.Random | None = None) -> List[str]:
    """
    7p Classic manifest: 1 TI + 1 TP + 1 TS + 1 TK + Mobster + NB + (NE|NK|Chaos).
    All roles unique (Classic). Faction counts match ``game_roles.mafia_neutral_counts(7)``.
    """
    if player_count != 7:
        raise ValueError(f"ToS Classic slot manifest is implemented for 7p only (got {player_count})")

    rng = rng or random.Random()
    all_unique = {r for r, _ in TOWN_RANDOM} | {"Mobster"} | {r for r, _ in NEUTRAL_ALL}
    roles: List[str] = []
    for slot in TOS_CLASSIC_7P_TOWN_SLOTS:
        role = _pick_weighted(_CATEGORY_POOLS[slot], chosen=set(), rng=rng, roles_so_far=roles, unique_roles=all_unique)
        roles.append(role)

    roles.append("Mobster")

    n_benign = _pick_weighted(NEUTRAL_BENIGN, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=all_unique)
    roles.append(n_benign)
    n_other = _pick_neutral_second(roles, rng, unique_roles=all_unique)
    roles.append(n_other)

    if len(roles) != player_count:
        raise ValueError(f"Role count mismatch: {len(roles)} != {player_count}")
    if len(set(roles)) != len(roles):
        raise ValueError(f"Duplicate roles in Classic draw: {roles}")
    return roles


def sample_tos_rt_dupe_roles(player_count: int, *, rng: random.Random | None = None) -> List[str]:
    """
    Casual TI/TP/RT-dupe manifests (dupes OK except globally unique roles).

    - 5p: delegates to ``sample_5p_no_neutral_ti_tp_rt2`` (1 TI + 1 TP + 2 RT + Mobster, no neutral)
    - 6p: 1 TI + 1 TP + 2 RT + Mobster + 1 neutral
    - 7p: 1 TI + 1 TP + 2 RT + Mobster + 2 neutrals (distinct buckets)

    Neutrals: bucket draw (25% per bucket); two slots use distinct buckets.
    """
    if player_count == 5:
        return sample_5p_no_neutral_ti_tp_rt2(rng=rng)

    if player_count not in _RT_DUPE_RT_SLOT_COUNT:
        raise ValueError(
            f"ToS RT-dupe manifest is implemented for 5p/6p/7p only (got {player_count})"
        )

    rng = rng or random.Random()
    rt_slots = _RT_DUPE_RT_SLOT_COUNT[player_count]
    num_neutral = _RT_DUPE_NEUTRAL_COUNT[player_count]
    roles: List[str] = []

    ti = _pick_weighted(TOWN_INVESTIGATIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    roles.append(ti)
    tp = _pick_weighted(TOWN_PROTECTIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    roles.append(tp)
    for _ in range(rt_slots):
        rt = _pick_weighted(TOWN_RANDOM, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
        roles.append(rt)

    roles.append("Mobster")

    _append_casual_neutrals(
        roles,
        rng,
        unique_roles=UNIQUE_ROLES_RT_DUPE,
        num_neutral=num_neutral,
        allowed_roles=_neutral_allowed_roles(player_count),
    )

    if len(roles) != player_count:
        raise ValueError(f"Role count mismatch: {len(roles)} != {player_count}")
    return roles


def sample_5p_no_neutral_random_town(*, rng: random.Random | None = None) -> List[str]:
    """5p MC experiment: 4 town from ``start_pool(5)`` manifest weights + Mobster, no neutral."""
    import game_roles as gr

    rng = rng or random.Random()
    pool = gr.start_pool_for_player_count(5, rng=rng)
    roles = gr.get_weighted_roles(pool.town_weights, 4, rng=rng)
    roles.append("Mobster")
    if len(roles) != 5:
        raise ValueError(f"Role count mismatch: {len(roles)} != 5")
    return roles


def sample_5p_no_neutral_ti_tp_rt2(*, rng: random.Random | None = None) -> List[str]:
    """5p: 1 TI + 1 TP + 2 RT + Mobster, no neutral."""
    rng = rng or random.Random()
    roles: List[str] = []
    roles.append(
        _pick_weighted(TOWN_INVESTIGATIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    )
    roles.append(
        _pick_weighted(TOWN_PROTECTIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    )
    for _ in range(2):
        roles.append(
            _pick_weighted(TOWN_RANDOM, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
        )
    roles.append("Mobster")
    if len(roles) != 5:
        raise ValueError(f"Role count mismatch: {len(roles)} != 5")
    return roles


def sample_5p_no_neutral_ti2_tp_rt(*, rng: random.Random | None = None) -> List[str]:
    """5p: 2 TI + 1 TP + 1 RT + Mobster, no neutral."""
    rng = rng or random.Random()
    roles: List[str] = []
    for _ in range(2):
        roles.append(
            _pick_weighted(TOWN_INVESTIGATIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
        )
    roles.append(
        _pick_weighted(TOWN_PROTECTIVE, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    )
    roles.append(
        _pick_weighted(TOWN_RANDOM, chosen=set(), rng=rng, roles_so_far=roles, unique_roles=UNIQUE_ROLES_RT_DUPE)
    )
    roles.append("Mobster")
    if len(roles) != 5:
        raise ValueError(f"Role count mismatch: {len(roles)} != 5")
    return roles


def has_duplicate_roles(roles: Sequence[str]) -> bool:
    return len(set(roles)) != len(roles)


def duplicate_role_names(roles: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    dupes: List[str] = []
    for r in roles:
        if r in seen and r not in dupes:
            dupes.append(r)
        seen.add(r)
    return dupes


def town_category_counts(roles: Sequence[str]) -> Dict[str, int]:
    """Count how many town roles fall in each Classic category (for tests / MC audit)."""
    inv = sum(1 for r in roles if r in {x for x, _ in TOWN_INVESTIGATIVE})
    prot = sum(1 for r in roles if r in {x for x, _ in TOWN_PROTECTIVE})
    sup = sum(1 for r in roles if r in {x for x, _ in TOWN_SUPPORT})
    kill = sum(1 for r in roles if r in {x for x, _ in TOWN_KILLING})
    return {
        "town_investigative": inv,
        "town_protective": prot,
        "town_support": sup,
        "town_killing": kill,
    }

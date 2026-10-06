"""Role pool configuration and neutral draw (Spec 0)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set, Tuple

import discord

CategoryPool = List[Tuple[str, int]]
NeutralBucket = str

@dataclass
class StartPool:
    town_weights: List[Tuple[str, int]]
    mafia_support_weights: List[Tuple[str, int]]
    neutral_pool_for_draw: List[str]
    neutral_pool_for_display: List[str]
    num_town: int
    num_mafia: int
    num_neutral: int
    player_count: int


NEUTRAL_BENIGN_POOL: CategoryPool = [
    ("Survivor", 8),
    ("Guardian Angel", 8),
]
NEUTRAL_EVIL_POOL: CategoryPool = [
    ("Jester", 8),
    ("Executioner", 8),
    ("Witch", 8),
]
NEUTRAL_KILLING_POOL: CategoryPool = [
    ("Arsonist", 6),
    ("Serial Killer", 6),
]
NEUTRAL_CHAOS_POOL: CategoryPool = [
    ("Pirate", 6),
    ("Chaos", 5),
]

NEUTRAL_BUCKET_ORDER: Tuple[NeutralBucket, ...] = (
    "neutral_benign",
    "neutral_evil",
    "neutral_killing",
    "neutral_chaos",
)
NEUTRAL_BUCKET_WEIGHTS: Dict[NeutralBucket, int] = {
    "neutral_benign": 25,
    "neutral_evil": 25,
    "neutral_killing": 25,
    "neutral_chaos": 25,
}
NEUTRAL_BUCKET_POOLS: Dict[NeutralBucket, CategoryPool] = {
    "neutral_benign": NEUTRAL_BENIGN_POOL,
    "neutral_evil": NEUTRAL_EVIL_POOL,
    "neutral_killing": NEUTRAL_KILLING_POOL,
    "neutral_chaos": NEUTRAL_CHAOS_POOL,
}

NEUTRAL_EVIL_ROLES: Set[str] = frozenset(r for r, _ in NEUTRAL_EVIL_POOL)
NEUTRAL_KILLING_ROLES: Set[str] = frozenset(r for r, _ in NEUTRAL_KILLING_POOL)
NEUTRAL_CHAOS_ROLES: Set[str] = frozenset(r for r, _ in NEUTRAL_CHAOS_POOL)
NEUTRAL_BENIGN_ROLES: Set[str] = frozenset(r for r, _ in NEUTRAL_BENIGN_POOL)

KILLING_NEUTRALS = NEUTRAL_KILLING_ROLES
UNIQUE_NEUTRAL_ROLES = frozenset({"Pirate", "Guardian Angel"})
# At most one per lobby; all other roles may duplicate (e.g. double Doctor).
LOBBY_UNIQUE_ROLES: frozenset[str] = frozenset(
    {"Mayor", "Scary Grandma", "Retributionist", "Mobster", "Pirate", "Arsonist"}
)


def globally_unique_roles() -> Set[str]:
    """Roles that may appear at most once per lobby."""
    return set(LOBBY_UNIQUE_ROLES) | set(UNIQUE_NEUTRAL_ROLES)


def lobby_duplicate_violations(roles: Sequence[str]) -> List[str]:
    """Unique roles that appear more than once (illegal lobby)."""
    return sorted({r for r in globally_unique_roles() if roles.count(r) > 1})


def _bracket_neutral_display(player_count: int) -> List[str]:
    """Display list for legacy 8p+ flat pool; 5p uses no neutral, 6p/7p use full buckets."""
    if player_count == 5:
        return []
    if player_count <= 7:
        return _full_neutral_role_names()
    return _full_neutral_role_names()


def _full_neutral_role_names() -> List[str]:
    return sorted({r for pool in NEUTRAL_BUCKET_POOLS.values() for r, _w in pool})


def _manifest_town_weights() -> List[Tuple[str, int]]:
    from game_roles_tos import TOWN_RANDOM

    return list(TOWN_RANDOM)


# Town slots in 5p/6p/7p TI+TP+2RT manifests (not used for 8p+ legacy draw).
MANIFEST_TOWN_SLOT_COUNT: int = 4


def mafia_neutral_counts(n: int) -> Tuple[int, int]:
    """Faction slot counts by player count (Spec 0)."""
    if n == 5:
        return 1, 0
    if n == 6:
        return 1, 1
    if n == 7:
        return 1, 2
    if n <= 9:
        return 2, 1
    if n <= 12:
        return 3, 2
    return 4, 2


def neutral_combo_draw_legal(neutrals: Sequence[str]) -> bool:
    """True if this neutral set can be produced by draw_neutrals (no dupes, slot caps)."""
    if len(neutrals) != len(set(neutrals)):
        return False
    if sum(1 for r in neutrals if r in KILLING_NEUTRALS) > 1:
        return False
    return True


def _pick_weighted_from_pool(
    pool: CategoryPool,
    *,
    rng: random.Random,
    roles_so_far: Sequence[str],
    unique_roles: Set[str],
) -> str:
    available: CategoryPool = []
    for role, weight in pool:
        if role in unique_roles and role in roles_so_far:
            continue
        available.append((role, weight))
    if not available:
        raise ValueError(f"Neutral bucket exhausted: {pool!r}")
    names = [r for r, _w in available]
    weights = [w for _r, w in available]
    return rng.choices(names, weights=weights, k=1)[0]


def _eligible_buckets(allowed_roles: Set[str]) -> Tuple[List[NeutralBucket], List[int]]:
    buckets: List[NeutralBucket] = []
    weights: List[int] = []
    for bucket in NEUTRAL_BUCKET_ORDER:
        pool = NEUTRAL_BUCKET_POOLS[bucket]
        if any(role in allowed_roles for role, _w in pool):
            buckets.append(bucket)
            weights.append(NEUTRAL_BUCKET_WEIGHTS[bucket])
    return buckets, weights


def draw_distinct_neutral_buckets(
    num_neutral: int,
    *,
    allowed_roles: Set[str],
    rng: random.Random,
    unique_roles: Set[str] | None = None,
) -> List[str]:
    """
    Pick ``num_neutral`` distinct neutral buckets (25% weight each), then one weighted role per bucket.

    Used by live ``draw_neutrals`` and MC ``tos-rt-dupe`` neutral slots.
    """
    if num_neutral <= 0:
        return []
    unique = unique_roles if unique_roles is not None else set(UNIQUE_NEUTRAL_ROLES)
    buckets, weights = _eligible_buckets(allowed_roles)
    if len(buckets) < num_neutral:
        raise ValueError(
            f"Not enough neutral buckets for {num_neutral} slot(s) (eligible={buckets!r})"
        )

    chosen_buckets: List[NeutralBucket] = []
    remaining = list(buckets)
    rem_weights = list(weights)
    for _ in range(num_neutral):
        bucket = rng.choices(remaining, weights=rem_weights, k=1)[0]
        chosen_buckets.append(bucket)
        idx = remaining.index(bucket)
        remaining.pop(idx)
        rem_weights.pop(idx)

    roles: List[str] = []
    for bucket in chosen_buckets:
        pool = [(r, w) for r, w in NEUTRAL_BUCKET_POOLS[bucket] if r in allowed_roles]
        roles.append(
            _pick_weighted_from_pool(pool, rng=rng, roles_so_far=roles, unique_roles=unique)
        )
    return roles


def start_pool_for_player_count(n: int, *, rng: random.Random) -> StartPool:
    del rng
    if n == 5:
        num_mafia, num_neutral = mafia_neutral_counts(n)
        num_town = MANIFEST_TOWN_SLOT_COUNT
        town_weights = _manifest_town_weights()
        mafia_support_weights: List[Tuple[str, int]] = []
        display_neutrals = []
        neutral_pool: List[str] = []
    elif n in (6, 7):
        num_mafia, num_neutral = mafia_neutral_counts(n)
        num_town = MANIFEST_TOWN_SLOT_COUNT
        town_weights = _manifest_town_weights()
        mafia_support_weights = []
        display_neutrals = _full_neutral_role_names()
        neutral_pool = list(display_neutrals)
    else:
        town_weights = [
            ("Doctor", 8),
            ("Sheriff", 8),
            ("Investigator", 6),
            ("Lookout", 7),
            ("Tracker", 7),
            ("Escort", 7),
            ("Bodyguard", 6),
            ("Vigilante", 6),
            ("Scary Grandma", 5),
            ("Transporter", 4),
            ("Mayor", 3),
            ("Retributionist", 3),
            ("Psychic", 5),
            ("Deputy", 4),
            ("Seer", 5),
        ]
        mafia_support_weights = [
            ("Gatekeeper", 8),
            ("Consort", 8),
            ("Framer", 7),
            ("Gravedigger", 6),
            ("Hypnotist", 5),
            ("Mole", 5),
            ("Tailor", 4),
        ]
        display_neutrals = _bracket_neutral_display(n)
        neutral_pool = list(display_neutrals)

        num_mafia, num_neutral = mafia_neutral_counts(n)
        num_town = n - num_mafia - num_neutral

    if (n - num_mafia) <= 1 and "Executioner" in neutral_pool:
        neutral_pool = [r for r in neutral_pool if r != "Executioner"]

    return StartPool(
        town_weights=town_weights,
        mafia_support_weights=mafia_support_weights,
        neutral_pool_for_draw=neutral_pool,
        neutral_pool_for_display=display_neutrals,
        num_town=num_town,
        num_mafia=num_mafia,
        num_neutral=num_neutral,
        player_count=n,
    )


def draw_roles_for_startgame(player_count: int, *, rng: random.Random | None = None) -> List[str]:
    """
    Prod role lineup for ``!startgame``.

    - **5p:** 1 TI + 1 TP + 2 RT + Mobster, no Neutral.
    - **6p / 7p:** 1 TI + 1 TP + 2 RT + Mobster + neutrals (full bucket pools).
    - **8p+:** legacy flat weighted pool + ``draw_neutrals``.
    """
    draw_rng = rng or random.Random()
    if player_count == 5:
        from game_roles_tos import sample_5p_no_neutral_ti_tp_rt2

        return sample_5p_no_neutral_ti_tp_rt2(rng=draw_rng)
    if player_count in (6, 7):
        from game_roles_tos import sample_tos_rt_dupe_roles

        return sample_tos_rt_dupe_roles(player_count, rng=draw_rng)

    pool = start_pool_for_player_count(player_count, rng=draw_rng)
    roles: List[str] = list(draw_neutrals(pool, rng=draw_rng))
    roles.extend(
        get_weighted_roles(pool.town_weights, pool.num_town, rng=draw_rng, roles_so_far=roles)
    )
    if pool.num_mafia > 0:
        mafia_roles = ["Mobster"]
        if pool.num_mafia > 1:
            mafia_roles.extend(
                get_weighted_roles(
                    pool.mafia_support_weights,
                    pool.num_mafia - 1,
                    rng=draw_rng,
                    roles_so_far=roles + mafia_roles,
                )
            )
        roles.extend(mafia_roles)
    return roles


def _append_rt_dupe_manifest_embed_fields(embed: discord.Embed, *, num_neutral: int) -> None:
    from game_roles_tos import TOWN_INVESTIGATIVE, TOWN_PROTECTIVE, TOWN_RANDOM

    embed.add_field(
        name="Town Investigative (1)",
        value="\n".join(_weight_lines(TOWN_INVESTIGATIVE)),
        inline=False,
    )
    embed.add_field(
        name="Town Protective (1)",
        value="\n".join(_weight_lines(TOWN_PROTECTIVE)),
        inline=False,
    )
    embed.add_field(
        name="Town Random (2)",
        value="\n".join(_weight_lines(TOWN_RANDOM)),
        inline=False,
    )
    embed.add_field(name="Mafia", value="Mobster — guaranteed", inline=False)
    if num_neutral > 0:
        neutral_display = ", ".join(_full_neutral_role_names())
        embed.add_field(
            name=f"Neutral ({num_neutral})",
            value=neutral_display,
            inline=False,
        )


def format_startgame_preview_embed(player_count: int) -> discord.Embed | None:
    """Lobby preview for 5p–7p manifests; ``None`` => use legacy ``format_role_pool_embed`` (8p+)."""
    dupes_note = (
        "Dupes allowed except globally unique roles "
        "(Mayor, Scary Grandma, RT, Mobster, Pirate, Arsonist, GA)."
    )
    if player_count == 5:
        embed = discord.Embed(
            title="Possible roles for 5 players",
            description=(
                "**Manifest:** 1 TI + 1 TP + 2 RT + **Mobster** — **no Neutral**.\n\n"
                f"{dupes_note}"
            ),
            color=discord.Color.dark_gold(),
        )
        _append_rt_dupe_manifest_embed_fields(embed, num_neutral=0)
        embed.set_footer(text="Preview — exact roles are drawn at !startgame.")
        return embed
    if player_count == 6:
        embed = discord.Embed(
            title="Possible roles for 6 players",
            description=(
                "**Manifest:** 1 TI + 1 TP + 2 RT + **Mobster** + **1 Neutral**.\n\n"
                "Neutral: one random bucket (25% each), then weighted within that bucket.\n\n"
                f"{dupes_note}"
            ),
            color=discord.Color.dark_gold(),
        )
        _append_rt_dupe_manifest_embed_fields(embed, num_neutral=1)
        embed.set_footer(text="Preview — exact roles are drawn at !startgame.")
        return embed
    if player_count == 7:
        embed = discord.Embed(
            title="Possible roles for 7 players",
            description=(
                "**Manifest:** 1 TI + 1 TP + 2 RT + **Mobster** + **2 Neutrals**.\n\n"
                "Neutrals: two distinct buckets (25% each), then weighted within each bucket.\n\n"
                f"{dupes_note}"
            ),
            color=discord.Color.dark_gold(),
        )
        _append_rt_dupe_manifest_embed_fields(embed, num_neutral=2)
        embed.set_footer(text="Preview — exact roles are drawn at !startgame.")
        return embed
    return None


def draw_neutrals(pool: StartPool, *, rng: random.Random) -> List[str]:
    """Distinct neutral buckets (25% each), one weighted role per bucket.

    At most one Witch or Executioner per game (see ``UNIQUE_NEUTRAL_ROLES`` / pool filtering).
    """
    allowed = set(pool.neutral_pool_for_draw)
    chosen = draw_distinct_neutral_buckets(
        pool.num_neutral,
        allowed_roles=allowed,
        rng=rng,
        unique_roles=set(UNIQUE_NEUTRAL_ROLES),
    )
    if len(chosen) < pool.num_neutral:
        raise ValueError(f"neutral draw failed: {len(chosen)} < {pool.num_neutral}")
    return chosen


def _weight_lines(pool: Sequence[Tuple[str, int]]) -> List[str]:
    total = sum(w for _, w in pool) or 1
    lines = []
    for role, w in sorted(pool, key=lambda x: -x[1]):
        pct = round(100 * w / total)
        lines.append(f"{role} — {pct}% ({w})")
    return lines


def format_role_pool_embed(pool: StartPool, *, roles_assigned: bool = True) -> discord.Embed:
    n = pool.player_count
    embed = discord.Embed(
        title=f"Possible roles for {n} players",
        description=(
            f"This game uses {pool.num_town} Town, {pool.num_mafia} Mafia, "
            f"{pool.num_neutral} Neutral role(s).\n\n"
            "Roles below can appear; weights are relative (higher = more likely)."
        ),
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="Town", value="\n".join(_weight_lines(pool.town_weights)) or "—", inline=False)
    if pool.num_mafia <= 1:
        mafia_lines = ["Mobster — guaranteed (solo Mafia at this player count)"]
    else:
        mafia_lines = ["Mobster — guaranteed"] + _weight_lines(pool.mafia_support_weights)
    embed.add_field(name="Mafia", value="\n".join(mafia_lines), inline=False)
    neutral_display = ", ".join(pool.neutral_pool_for_display)
    if pool.num_neutral > 0:
        neutral_notes = (
            f"This lobby uses {pool.num_neutral} Neutral role(s).\n"
            "Neutrals: distinct buckets per slot (NB / NE / NK / NC at 25% each), "
            "one weighted role per bucket when multiple slots.\n"
            "Duplicate roles are allowed except: Mayor, Scary Grandma, Retributionist, Mobster, "
            "Pirate, Arsonist, and Guardian Angel (at most one each)."
        )
        embed.add_field(
            name="Neutral",
            value=f"{neutral_display}\n\n{neutral_notes}",
            inline=False,
        )
    if roles_assigned:
        footer = "Day 1\nThe game has begun. Roles have been assigned."
    else:
        footer = "Preview for current lobby size — exact roles are drawn at !startgame."
    embed.set_footer(text=footer)
    return embed


def get_weighted_roles(
    role_pool: Sequence[Tuple[str, int]],
    count: int,
    *,
    rng: random.Random,
    roles_so_far: Sequence[str] | None = None,
    unique_roles: Set[str] | None = None,
) -> List[str]:
    """Draw ``count`` roles with replacement; skip picks already taken from ``unique_roles``."""
    unique = unique_roles if unique_roles is not None else globally_unique_roles()
    taken = list(roles_so_far) if roles_so_far is not None else []
    selected: List[str] = []
    for _ in range(count):
        available: CategoryPool = [
            (role, weight)
            for role, weight in role_pool
            if not (role in unique and role in taken)
        ]
        if not available:
            raise ValueError(f"Role pool exhausted under unique caps: {role_pool!r}")
        names = [r for r, _w in available]
        weights = [w for _r, w in available]
        chosen = rng.choices(names, weights=weights, k=1)[0]
        selected.append(chosen)
        taken.append(chosen)
    return selected

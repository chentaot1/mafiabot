"""Attack / defense tiers for night (and day) combat resolution.

ToS1 rule: kill when ``attack_tier > defense_tier``. ``invincible`` blocks every attack
(lynch still applies). Unstoppable pierces up to ``powerful`` but not ``invincible``.

Passive role defense and action attack levels live in ``config.py`` for balance edits.
"""
from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING, Optional

from config import (
    ATTACK_TIER_BY_NIGHT_ACTION,
    BODYGUARD_COUNTER_ATTACK_TIER,
    DEPUTY_DAY_ATTACK_TIER,
    HEAL_DEFENSE_TIER,
    IGNITE_ATTACK_TIER,
    N1_BASIC_SHIELD_ROLES,
    NIGHT_DEFENSE_BY_ROLE_STATE,
    ROLE_PASSIVE_DEFENSE,
    SCARY_GRANDMA_ALERT_ATTACK_TIER,
)

if TYPE_CHECKING:
    from game import Game


class CombatTier(IntEnum):
    NONE = 0
    BASIC = 1
    POWERFUL = 2
    UNSTOPPABLE = 3
    INVINCIBLE = 4


_TIER_BY_NAME = {
    "none": CombatTier.NONE,
    "basic": CombatTier.BASIC,
    "powerful": CombatTier.POWERFUL,
    "unstoppable": CombatTier.UNSTOPPABLE,
    "invincible": CombatTier.INVINCIBLE,
}


def tier_from_name(name: str) -> CombatTier:
    try:
        return _TIER_BY_NAME[str(name).strip().lower()]
    except KeyError as e:
        raise ValueError(f"Unknown combat tier: {name!r}") from e


def passive_defense_for_role(role: Optional[str]) -> CombatTier:
    if not role:
        return CombatTier.NONE
    return tier_from_name(ROLE_PASSIVE_DEFENSE.get(role, "none"))


_KILL_ACTION_TYPES = frozenset({"kill", "shoot", "sk_kill", "plunder", "sk_counter"})


def attack_tier_for_night_action(action_type: str) -> CombatTier:
    tier_name = ATTACK_TIER_BY_NIGHT_ACTION.get(action_type, "none")
    if action_type in _KILL_ACTION_TYPES and tier_name == "none":
        raise ValueError(f"Kill action {action_type!r} must have a combat tier in ATTACK_TIER_BY_NIGHT_ACTION")
    return tier_from_name(tier_name)


def bodyguard_counter_attack_tier() -> CombatTier:
    return tier_from_name(BODYGUARD_COUNTER_ATTACK_TIER)


def deputy_day_attack_tier() -> CombatTier:
    return tier_from_name(DEPUTY_DAY_ATTACK_TIER)


def ignite_attack_tier() -> CombatTier:
    return tier_from_name(IGNITE_ATTACK_TIER)


def scary_grandma_alert_attack_tier() -> CombatTier:
    return tier_from_name(SCARY_GRANDMA_ALERT_ATTACK_TIER)


def attack_pierces_defense(attack: CombatTier, defense: CombatTier) -> bool:
    """True if this attack should deal a killing blow."""
    if defense == CombatTier.INVINCIBLE:
        return False
    return int(attack) > int(defense)


def effective_night_defense(
    game: "Game",
    target_id: int,
    *,
    healed: bool = False,
) -> CombatTier:
    """Passive + per-night state defense (vest, alert, GA ward, Doctor heal)."""
    role = game.player_roles.get(target_id)
    tier = passive_defense_for_role(role)
    state = game.role_states.get(target_id, {}) or {}

    if healed:
        tier = max(tier, tier_from_name(HEAL_DEFENSE_TIER), key=int)
    for state_key, tier_name in NIGHT_DEFENSE_BY_ROLE_STATE.items():
        if state.get(state_key):
            tier = max(tier, tier_from_name(tier_name), key=int)
    return tier


def night_attack_would_kill(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
) -> bool:
    defense = effective_night_defense(game, target_id, healed=healed)
    return attack_pierces_defense(attack, defense)


def n1_basic_shield_available(game: "Game", target_id: int) -> bool:
    if int(getattr(game, "day_number", 0)) != 1:
        return False
    role = game.player_roles.get(target_id)
    if role not in N1_BASIC_SHIELD_ROLES:
        return False
    return not bool(game.role_states.get(target_id, {}).get("night1_shield_used"))


def n1_basic_shield_blocks(attack: CombatTier) -> bool:
    """ToS1 N1 shields block basic attacks only (not powerful / unstoppable)."""
    return attack == CombatTier.BASIC


def normal_night_attack_lethal(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
) -> bool:
    """True if a normal (non-ignite/alert) night attack should kill after N1 shields."""
    if not night_attack_would_kill(game, target_id, attack, healed=healed):
        return False
    if n1_basic_shield_available(game, target_id) and n1_basic_shield_blocks(attack):
        return False
    return True


def normal_night_attack_blocked(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
    for_attacker_feedback: bool = False,
) -> bool:
    """True when the target survives this attack tier (defense tier or N1 basic shield)."""
    if not night_attack_would_kill(game, target_id, attack, healed=healed):
        return True
    if n1_basic_shield_available(game, target_id) and n1_basic_shield_blocks(attack):
        return True
    if for_attacker_feedback and n1_basic_shield_blocks(attack):
        if game.role_states.get(target_id, {}).get("night1_shield_used"):
            return True
    return False


def _defense_without_state_flag(
    game: "Game",
    target_id: int,
    state_key: str,
    *,
    healed: bool = False,
) -> CombatTier:
    """Effective defense if ``state_key`` were not active on the target."""
    role = game.player_roles.get(target_id)
    tier = passive_defense_for_role(role)
    state = dict(game.role_states.get(target_id, {}) or {})
    state.pop(state_key, None)
    if healed:
        tier = max(tier, tier_from_name(HEAL_DEFENSE_TIER), key=int)
    for key, tier_name in NIGHT_DEFENSE_BY_ROLE_STATE.items():
        if state.get(key):
            tier = max(tier, tier_from_name(tier_name), key=int)
    return tier


def apply_n1_basic_shield_if_blocked(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
) -> bool:
    """Consume N1 basic shield and set ``attacked_tonight_reason``; DM sent in ``send_night_feedback``."""
    if not night_attack_would_kill(game, target_id, attack, healed=healed):
        return False
    if not n1_basic_shield_available(game, target_id) or not n1_basic_shield_blocks(attack):
        return False
    state = game.role_states.setdefault(target_id, {})
    state["night1_shield_used"] = True
    role = game.player_roles.get(target_id)
    if role == "Witch":
        state["attacked_tonight_reason"] = "witch_shield"
    elif role == "Chaos":
        state["attacked_tonight_reason"] = "chaos_shield"
    else:
        state["attacked_tonight_reason"] = "jester_shield"
    return True


def record_non_lethal_kill_outcome(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
) -> bool:
    """Apply N1 shield or tier survival reason after ``normal_night_attack_lethal`` is false.

    Returns True if the target survives this attack attempt.
    """
    if apply_n1_basic_shield_if_blocked(game, target_id, attack, healed=healed):
        return True
    if not night_attack_would_kill(game, target_id, attack, healed=healed):
        set_survived_attack_reason(game, target_id, attack, healed=healed)
        return True
    return False


def set_survived_attack_reason(
    game: "Game",
    target_id: int,
    attack: CombatTier,
    *,
    healed: bool = False,
) -> None:
    """Set ``attacked_tonight_reason`` after a failed kill (tier math; N1 uses ``apply_n1_basic_shield_if_blocked``)."""
    state = game.role_states.setdefault(target_id, {})
    if healed and not night_attack_would_kill(game, target_id, attack, healed=True):
        state["attacked_tonight_reason"] = "healed"
        return
    if state.get("ga_shield_active_tonight") and not night_attack_would_kill(
        game, target_id, attack, healed=healed
    ):
        state["attacked_tonight_reason"] = "ga_ward"
        return
    if state.get("is_vested"):
        without = _defense_without_state_flag(game, target_id, "is_vested", healed=healed)
        if attack_pierces_defense(attack, without) and not attack_pierces_defense(
            attack, effective_night_defense(game, target_id, healed=healed)
        ):
            state["attacked_tonight_reason"] = "vest"
            return
    if state.get("is_on_alert"):
        without = _defense_without_state_flag(game, target_id, "is_on_alert", healed=healed)
        if attack_pierces_defense(attack, without) and not attack_pierces_defense(
            attack, effective_night_defense(game, target_id, healed=healed)
        ):
            state["attacked_tonight_reason"] = "alert"
            return
    if attack == CombatTier.UNSTOPPABLE and not night_attack_would_kill(
        game, target_id, attack, healed=healed
    ):
        state["attacked_tonight_reason"] = "ignite_blocked"
        return
    state["attacked_tonight_reason"] = "survived"


def deputy_shot_would_kill_target(game: "Game", target_id: int) -> bool:
    """Deputy daytime gun vs target passive / vest defense (ignores night heal — day only)."""
    return night_attack_would_kill(game, target_id, deputy_day_attack_tier(), healed=False)


def max_attack_tier_for_target(
    attempted_kills: list[tuple[int, int, str]],
    target_id: int,
) -> CombatTier:
    tier = CombatTier.NONE
    for _actor, tvid, a_type in attempted_kills:
        if tvid != target_id:
            continue
        tier = max(tier, attack_tier_for_night_action(a_type), key=int)
    return tier


_NIGHT_DEATH_CAUSE_BY_ACTION: dict[str, str] = {
    "kill": "mafia",
    "shoot": "vigilante",
    "sk_kill": "serial_killer",
    "plunder": "pirate_plunder",
    "sk_counter": "sk_counter_attack",
}


def night_death_cause_for_action(action_type: str) -> str:
    return _NIGHT_DEATH_CAUSE_BY_ACTION.get(action_type, "night_kill")


def primary_kill_attacker_for_target(
    attempted_kills: list[tuple[int, int, str]],
    target_id: int,
) -> tuple[int, str] | None:
    """Actor + action type for the highest-tier attack on ``target_id``.

    Same-tier ties: first entry in ``attempted_kills`` wins (stable night_actions order).
    """
    best: tuple[int, str] | None = None
    best_tier = CombatTier.NONE
    for actor_id, tvid, a_type in attempted_kills:
        if tvid != target_id:
            continue
        tier = attack_tier_for_night_action(a_type)
        if int(tier) > int(best_tier):
            best_tier = tier
            best = (actor_id, a_type)
    return best

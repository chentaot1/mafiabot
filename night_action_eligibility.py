"""Single source of truth for limited night-action eligibility (commands + engine)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from config import ALL_MAFIA_ROLES
from persist_schema import coerce_role_state_int

if TYPE_CHECKING:
    from game import Game

# Witch cannot retarget self-only / Bodyguard self-defense actions (ToS-like).
WITCH_NON_RETARGETABLE_ACTION_TYPES = frozenset({"vest", "clean", "ward", "bg_vest"})

FRAMER_LAST_FRAME_NIGHT = 2


def role_state_uses(state: Dict[str, Any], key: str = "uses_remaining") -> int:
    return coerce_role_state_int(state.get(key), 0)


def bodyguard_off_self_protect_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "uses_remaining") > 0


def bodyguard_self_vest_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "self_protects_remaining") > 0


def gravedigger_hide_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "uses_remaining") > 0


def mole_investigate_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "uses_remaining") > 0


def framer_frame_eligible(game: "Game") -> bool:
    return int(getattr(game, "day_number", 0)) <= FRAMER_LAST_FRAME_NIGHT


def survivor_vest_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "vests_remaining") > 0


def scary_grandma_alert_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "alerts_remaining") > 0


def retributionist_consume_eligible(game: "Game", actor_id: int) -> bool:
    state = game.role_states.get(actor_id, {})
    return role_state_uses(state, "uses_remaining") > 0


def gatekeeper_guard_target_allowed(
    game: "Game",
    gk_id: int,
    action: Dict[str, object],
    *,
    effective_target_id: Optional[int],
    back_to_back_rejects,
) -> bool:
    """Command-parity target rules for a guard row (no charge check)."""
    if effective_target_id is None:
        return False
    if int(effective_target_id) == int(gk_id):
        return False
    if game.player_roles.get(int(effective_target_id)) in ALL_MAFIA_ROLES:
        return False
    raw_target = action.get("target")
    try:
        submitted_target_id = int(raw_target)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if back_to_back_rejects(game, int(gk_id), submitted_target_id):
        return False
    return True


def gatekeeper_blocking_active(
    game: "Game",
    gk_id: int,
    action: Dict[str, object],
    *,
    effective_target_id: Optional[int],
    back_to_back_rejects,
) -> bool:
    """True when a guard should block visitors this night (may be recomputed after use spend)."""
    if action.get("_from_chaos"):
        return True
    if game.player_roles.get(gk_id) != "Gatekeeper":
        return False
    if not gatekeeper_guard_target_allowed(
        game, gk_id, action, effective_target_id=effective_target_id, back_to_back_rejects=back_to_back_rejects
    ):
        return False
    state = game.role_states.get(gk_id, {})
    if state.get("gatekeeper_used_this_night"):
        return True
    return role_state_uses(state, "uses_remaining") > 0


def gatekeeper_may_consume_use(
    game: "Game",
    gk_id: int,
    action: Dict[str, object],
    *,
    effective_target_id: Optional[int],
    back_to_back_rejects,
) -> bool:
    """True when a real Gatekeeper guard should decrement ``uses_remaining`` once."""
    if action.get("_from_chaos"):
        return False
    if game.player_roles.get(gk_id) != "Gatekeeper":
        return False
    state = game.role_states.get(gk_id, {})
    if state.get("gatekeeper_used_this_night"):
        return False
    if role_state_uses(state, "uses_remaining") <= 0:
        return False
    return gatekeeper_guard_target_allowed(
        game, gk_id, action, effective_target_id=effective_target_id, back_to_back_rejects=back_to_back_rejects
    )


def chaos_targets_valid(action: Dict[str, object]) -> Optional[tuple[int, int]]:
    targets = action.get("targets")
    if not isinstance(targets, list) or len(targets) != 2:
        return None
    try:
        t1, t2 = int(targets[0]), int(targets[1])
    except (TypeError, ValueError):
        return None
    if t1 == t2:
        return None
    return t1, t2


def chaos_may_consume_use(game: "Game", actor_id: int, action: Dict[str, object]) -> bool:
    if action.get("type") != "chaos":
        return False
    if chaos_targets_valid(action) is None:
        return False
    state = game.role_states.get(actor_id, {})
    if state.get("chaos_used_this_night"):
        return False
    return role_state_uses(state, "uses_remaining") > 0


def chaos_try_spend_use(game: "Game", actor_id: int, action: Dict[str, object]) -> bool:
    """Spend one Chaos use for a valid submitted chaos row (effect may still be blocked)."""
    if not chaos_may_consume_use(game, actor_id, action):
        return False
    state = game.role_states.setdefault(actor_id, {})
    state["uses_remaining"] = max(0, role_state_uses(state, "uses_remaining") - 1)
    state["chaos_used_this_night"] = True
    return True

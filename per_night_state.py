"""Single source of truth for ephemeral per-night ``role_states`` keys."""
from __future__ import annotations

from typing import FrozenSet, Tuple

# Cleared at pipeline entry (``engine/night``) and mirrored in checkpoint resume logic.
PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR: Tuple[str, ...] = (
    "is_framed",
    "is_vested",
    "is_on_alert",
    "pirate_win_this_night",
    "gatekeeper_used_this_night",
    "self_heal_used_this_night",
    "vest_used_this_night",
    "alert_used_this_night",
    "bg_protect_used_this_night",
    "bg_self_protect_used_this_night",
    "vig_shot_used_this_night",
    "mole_used_this_night",
    "tailor_used_this_night",
    "gravedigger_used_this_night",
    "chaos_visit_targets",
    "chaos_transport_pair",
    "chaos_protected_by",
    "investigative_sent_tonight",
    "attacked_tonight_reason",
    "psychic_vision_recipient_id",
    "ga_shield_active_tonight",
    "sk_suppressed_by_pirate",
)

# ``chaos_used_this_night`` uses a dedicated crash-recovery heuristic in ``run_night_pipeline``.
CHAOS_USED_THIS_NIGHT_KEY = "chaos_used_this_night"

# Cleared only at ``Game.start_night`` (night boundary), not mid-pipeline re-entry.
START_NIGHT_ONLY_CLEAR_KEYS: Tuple[str, ...] = (
    CHAOS_USED_THIS_NIGHT_KEY,
    "sk_counter_kills",
)

# Sim harness mirrors start_night + pipeline keys.
SIM_EXTRA_CLEAR_KEYS: Tuple[str, ...] = ("last_action_summary",)


def all_keys_cleared_at_start_night() -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (*PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR, *START_NIGHT_ONLY_CLEAR_KEYS, *SIM_EXTRA_CLEAR_KEYS)
        )
    )


# Preserved on crash-resume mid-pipeline (``night_engine_checkpoint`` subtracts these from clear).
PRESERVE_EXTRA_ROLE_STATE_KEYS_ON_RESUME: Tuple[str, ...] = (
    "is_hidden_by_gravedigger",
    CHAOS_USED_THIS_NIGHT_KEY,
)


def preserve_role_state_keys_on_resume() -> FrozenSet[str]:
    """All per-night keys plus multi-night / chaos-resume extras."""
    return frozenset(
        dict.fromkeys(
            (*PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR, *PRESERVE_EXTRA_ROLE_STATE_KEYS_ON_RESUME)
        )
    )

"""Action admission rules reconstructed from the live and recovery call sites."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from night_resume import normalize_night_completion_snapshot, parse_night_resume_state
from persist_schema import coerce_bool


def night_actions_frozen(game: Any) -> bool:
    """Keep submitted actions immutable during resolution and matching crash resume."""
    if coerce_bool(getattr(game, 'resolving', False)):
        return True
    guard = getattr(game, '_night_resolve_guard', None)
    if guard is not None and callable(getattr(guard, 'locked', None)) and guard.locked():
        return True
    if coerce_bool(getattr(game, 'ending', False)) or coerce_bool(getattr(game, 'cleanup_pending', False)):
        return True
    if getattr(game, 'phase', None) != 'night':
        return False
    snap = normalize_night_completion_snapshot(getattr(game, 'night_completion_snapshot', None))
    if snap is None:
        return False
    state = parse_night_resume_state(snap, day_number=game.day_number, game_key=game.game_key)
    return state.resuming and (state.resume_post_pipeline_only or state.resume_engine_incomplete)


def actor_has_guilt_pending(game: Any, actor_id: int) -> bool:
    """A shooter awaiting deferred guilt cannot submit another night ability."""
    states = getattr(game, 'role_states', {})
    if not isinstance(states, Mapping):
        return False
    state = states.get(actor_id, states.get(str(actor_id), {}))
    if not isinstance(state, Mapping):
        return False
    return coerce_bool(state.get('guilty_tomorrow', False)) or coerce_bool(state.get('will_die_of_guilt', False))

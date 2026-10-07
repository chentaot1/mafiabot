"""Expand Retributionist `reanimate` night actions before run_night_pipeline."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from game import Game

# ToS1 / MafiaSalem: corpses that expand to a night action (single living target).
RETRI_CORPSE_EXPANDABLE_ROLES = frozenset(
    {
        "Doctor",
        "Sheriff",
        "Investigator",
        "Lookout",
        "Tracker",
        "Escort",
        "Bodyguard",
        "Vigilante",
        "Transporter",
    }
)

# Explicit deny list (also enforced via expandable allow-list).
RETRI_CORPSE_DENIED_ROLES = frozenset(
    {
        "Mayor",
        "Retributionist",
        "Psychic",
        "Deputy",
        "Seer",
        "Scary Grandma",  # ToS1 Veteran analog — not resurrectable
    }
)


def retributionist_corpse_lists_for_docs() -> tuple[str, str]:
    """Sorted allow/deny labels for player-facing docs (single source)."""
    allowed = ", ".join(sorted(RETRI_CORPSE_EXPANDABLE_ROLES))
    denied = ", ".join(sorted(RETRI_CORPSE_DENIED_ROLES))
    return allowed, denied

# Backward-compatible alias for imports/tests.
SUPPORTED_RETRI_CORPSE_ROLES = RETRI_CORPSE_EXPANDABLE_ROLES

_CORPSE_ACTION_TYPES = {
    "Doctor": "heal", "Sheriff": "investigate", "Investigator": "investigate",
    "Lookout": "watch", "Tracker": "track", "Escort": "roleblock",
    "Bodyguard": "ret_protect", "Vigilante": "shoot", "Transporter": "transport",
}


def _player_id(value: object) -> Optional[int]:
    """Accept saved integer IDs without turning booleans/floats into player IDs."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result > 0 else None


def normalized_retributionist_action(
    game: "Game", actor_id: int, action: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Validate submitted and already-expanded corpse payloads, including recovery rows.

    This checks payload integrity, not whether the valid attempt succeeded. Uses
    are still spent for blocked/redundant attempts under the current role rules.
    """
    if not isinstance(action, dict) or game.player_roles.get(actor_id) != "Retributionist":
        return None
    if _player_id(action.get("actor", actor_id)) != actor_id:
        return None
    submitted = action.get("type") == "reanimate"
    corpse_id = _player_id(action.get("corpse_player_id" if submitted else "_from_retri"))
    if corpse_id is None:
        return None
    entry = graveyard_entry_for_corpse(game, corpse_id)
    if entry is None:
        return None
    role = entry.get("real_role")
    if not isinstance(role, str) or role not in _CORPSE_ACTION_TYPES:
        return None
    if submitted:
        if action.get("corpse_role") != role:
            return None
    elif action.get("type") != _CORPSE_ACTION_TYPES[role]:
        return None
    elif role in {"Sheriff", "Investigator"} and action.get("role") != role:
        return None

    living_ids = {p.id for p in game.living_players}
    if actor_id not in living_ids or corpse_id in living_ids:
        return None
    normalized = dict(action)
    normalized["actor"] = actor_id
    normalized["corpse_player_id" if submitted else "_from_retri"] = corpse_id
    if role == "Transporter":
        raw_targets = action.get("targets")
        if not isinstance(raw_targets, list) or len(raw_targets) != 2:
            return None
        targets = [_player_id(value) for value in raw_targets]
        if any(target not in living_ids for target in targets) or targets[0] == targets[1]:
            return None
        normalized["targets"] = targets
        if submitted:
            normalized["target"] = targets[0]
    else:
        target = _player_id(action.get("target"))
        if target not in living_ids:
            return None
        normalized["target"] = target
    return normalized


def is_retri_usable_corpse(
    game: "Game", entry: Dict[str, Any], *, retri_player_id: int
) -> bool:
    """True if graveyard entry can appear in ``!corpses`` / ``!reanimate``."""
    from config import TOWN_ROLES

    sync_retributionist_corpse_spent_state(game, retri_player_id)
    if entry.get("used_by_retri"):
        return False
    if entry.get("is_hidden"):
        return False
    pid = entry.get("player_id")
    if pid is None:
        return False
    role = entry.get("real_role")
    if not isinstance(role, str) or not role:
        return False
    if role in RETRI_CORPSE_DENIED_ROLES:
        return False
    if role not in RETRI_CORPSE_EXPANDABLE_ROLES:
        return False
    if role not in TOWN_ROLES:
        return False
    used_ids: set[int] = set()
    for x in (game.role_states.get(int(retri_player_id), {}) or {}).get(
        "used_corpses", []
    ):
        try:
            used_ids.add(int(x))
        except (TypeError, ValueError):
            continue
    try:
        if int(pid) in used_ids:
            return False
    except (TypeError, ValueError):
        return False
    return True


def list_usable_retributionist_corpses(
    game: "Game", *, retri_player_id: int
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in game.graveyard:
        if isinstance(entry, dict) and is_retri_usable_corpse(
            game, entry, retri_player_id=retri_player_id
        ):
            out.append(entry)
    return out


def graveyard_entry_for_corpse(game: "Game", corpse_pid: object) -> Optional[Dict[str, Any]]:
    """Return graveyard row for ``corpse_pid``, or None."""
    try:
        corpse_int = int(corpse_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for entry in game.graveyard:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("player_id")) == corpse_int:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def _normalize_used_corpses_list(raw: object) -> List[int]:
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def sync_retributionist_corpse_spent_state(game: "Game", retri_player_id: int) -> None:
    """Keep ``used_corpses`` and graveyard ``used_by_retri`` aligned for one Retributionist."""
    st = game.role_states.get(int(retri_player_id), {}) or {}
    used_ids = set(_normalize_used_corpses_list(st.get("used_corpses")))
    for entry in game.graveyard:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("player_id"))
        except (TypeError, ValueError):
            continue
        if entry.get("used_by_retri"):
            used_ids.add(pid)
            entry["used_by_retri"] = True
        elif pid in used_ids:
            entry["used_by_retri"] = True
    if used_ids:
        game.role_states.setdefault(int(retri_player_id), {})["used_corpses"] = sorted(used_ids)


def mark_retributionist_corpse_used(
    game: "Game", *, retri_player_id: int, corpse_player_id: int
) -> None:
    """Single write path for corpse consumption (live + MC)."""
    corpse_int = int(corpse_player_id)
    retri_int = int(retri_player_id)
    st = game.role_states.setdefault(retri_int, {})
    used = _normalize_used_corpses_list(st.get("used_corpses"))
    if corpse_int not in used:
        used.append(corpse_int)
    st["used_corpses"] = used
    for entry in game.graveyard:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("player_id")) == corpse_int:
                entry["used_by_retri"] = True
                break
        except (TypeError, ValueError):
            continue


def graveyard_real_role_for_corpse(game: "Game", corpse_pid: object) -> Optional[str]:
    entry = graveyard_entry_for_corpse(game, corpse_pid)
    if not entry:
        return None
    role = entry.get("real_role")
    return role if isinstance(role, str) and role else None


def reanimate_action_valid_for_expand(
    game: "Game", actor_id: int, action: Dict[str, Any]
) -> bool:
    """True when ``reanimate`` payload matches a usable graveyard corpse at expand time."""
    normalized = normalized_retributionist_action(game, actor_id, action)
    if normalized is None or normalized.get("type") != "reanimate":
        return False
    entry = graveyard_entry_for_corpse(game, normalized["corpse_player_id"])
    return is_retri_usable_corpse(game, entry, retri_player_id=int(actor_id))


def expand_reanimate_actions(game: "Game", *, strict: bool = False, for_execution: bool = True) -> List[int]:
    """
    Single source for bot ``!resolve``, MC bridge, and sim_test.

    Returns actor ids whose corpse payload was rejected (for player DMs).
    Invalid rows are removed before they can visit, execute, or consume resources.
    Already-expanded recovery rows receive the same payload checks.
    """
    failed_retri: List[int] = []
    for actor_id, action in list(game.night_actions.items()):
        if not isinstance(action, dict):
            continue
        submitted = action.get("type") == "reanimate"
        if not submitted and "_from_retri" not in action:
            continue
        normalized = normalized_retributionist_action(game, actor_id, action)
        eligible = True
        if for_execution and normalized is not None:
            from night_action_eligibility import retributionist_consume_eligible
            corpse_id = normalized.get('corpse_player_id', normalized.get('_from_retri'))
            eligible = (retributionist_consume_eligible(game, int(actor_id)) and
                        is_retri_usable_corpse(game, graveyard_entry_for_corpse(game, corpse_id), retri_player_id=int(actor_id)))
        if normalized is None or not eligible or (submitted and not reanimate_action_valid_for_expand(game, actor_id, normalized)):
            if strict:
                raise ValueError("expand_reanimate_actions: invalid or unusable corpse action")
            failed_retri.append(int(actor_id))
            game.night_actions.pop(actor_id, None)
            continue
        if not submitted:
            game.night_actions[actor_id] = normalized
            continue
        corpse_role = normalized["corpse_role"]
        expanded: Dict[str, Any] = {
            "type": _CORPSE_ACTION_TYPES[corpse_role], "actor": actor_id,
            "_from_retri": normalized["corpse_player_id"],
        }
        if corpse_role == "Transporter":
            expanded["targets"] = normalized["targets"]
        else:
            expanded["target"] = normalized["target"]
        if corpse_role in {"Sheriff", "Investigator"}:
            expanded["role"] = corpse_role
        game.night_actions[actor_id] = expanded
    return failed_retri


async def notify_retributionist_expand_failures(
    game: "Game", guild: object, failed_actor_ids: List[int]
) -> None:
    """DM Retributionists when ``reanimate`` did not expand (hidden/used/invalid corpse)."""
    if not failed_actor_ids:
        return
    from engine.night import _dm_player
    from messages import tos as tos_msg

    for actor_id in failed_actor_ids:
        if game.player_roles.get(actor_id) != "Retributionist":
            continue
        member = await game.get_member_safe(guild, actor_id)  # type: ignore[arg-type]
        if member:
            await _dm_player(member, tos_msg.retri_corpse_missing())


def append_retributionist_corpse_visits(
    game: "Game", visit_log: Dict[int, List[int]]
) -> None:
    """ToS1: Retributionist visits the corpse; the corpse visits the ability target."""
    living_ids_set = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    for actor_id, action in list(game.night_actions.items()):
        if game.player_roles.get(actor_id) != "Retributionist":
            continue
        raw_corpse = action.get("_from_retri")
        if raw_corpse is None:
            continue
        try:
            corpse_id = int(raw_corpse)
        except (TypeError, ValueError):
            continue
        if living_ids_set and actor_id in living_ids_set:
            visitors = visit_log.setdefault(corpse_id, [])
            if actor_id not in visitors:
                visitors.append(int(actor_id))
        from engine.night import effective_primary_target

        dest = effective_primary_target(game, int(actor_id))
        if dest is None:
            continue
        visitors = visit_log.setdefault(int(dest), [])
        if corpse_id not in visitors:
            visitors.append(corpse_id)

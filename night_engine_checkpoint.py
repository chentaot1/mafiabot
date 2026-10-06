"""Durable night-engine checkpoints (crash-resume) without importing bot_app."""
from __future__ import annotations

from typing import TYPE_CHECKING, Collection, Dict, List, Optional, Set

from night_resume import (
    coerce_snap_id_list,
    night_kill_deaths_from_snap,
    normalize_night_completion_snapshot,
)
from persist_schema import coerce_bool

if TYPE_CHECKING:
    from game import Game

from per_night_state import preserve_role_state_keys_on_resume

# Role-state keys preserved when resuming mid-pipeline (avoid double-spend / visit-log drift).
_PRESERVE_ROLE_STATE_KEYS_ON_RESUME: frozenset[str] = preserve_role_state_keys_on_resume()

_INVESTIGATIVE_ACTION_TYPES = frozenset({"investigate", "watch", "track", "gaze"})


def _merge_snap(game: "Game", patch: Dict[str, object]) -> Dict[str, object]:
    norm = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    base: Dict[str, object] = dict(norm) if norm is not None else {}
    if "day" not in base:
        base["day"] = int(game.day_number)
    if "game_key" not in base:
        gk = getattr(game, "game_key", None)
        if gk is not None:
            base["game_key"] = gk
    base.update(patch)
    game.night_completion_snapshot = base
    return base


def chaos_phase_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("chaos_phase_complete")))


def transport_control_phase_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("transport_control_complete")))


def killing_phase_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("killing_phase_complete")))


def blocked_from_snap(game: "Game") -> List[int]:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    if snap is None:
        return []
    return coerce_snap_id_list(snap, "blocked")


def gk_sk_witch_notify_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("gk_sk_witch_notify_complete")))


def deaths_from_killing_checkpoint(game: "Game") -> Set[int]:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    if snap is None:
        return set()
    engine = night_kill_deaths_from_snap(snap)
    if engine:
        return set(engine)
    return set(coerce_snap_id_list(snap, "deaths"))


def investigative_phase_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("investigative_phase_complete")))


def misc_phase_complete(game: "Game") -> bool:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    return bool(snap and coerce_bool(snap.get("misc_phase_complete")))


def resume_engine_in_progress(game: "Game") -> bool:
    """True when snap indicates an interrupted ``!resolve`` before engine completion."""
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    if snap is None:
        return False
    if coerce_bool(snap.get("night_engine_completed")) or coerce_bool(snap.get("post_pipeline_pending")):
        return False
    return coerce_bool(snap.get("pre_pipeline")) or coerce_bool(snap.get("night_engine_running"))


def should_preserve_role_state_on_resume(game: "Game") -> bool:
    if (
        transport_control_phase_complete(game)
        or chaos_phase_complete(game)
        or investigative_phase_complete(game)
        or misc_phase_complete(game)
        or killing_phase_complete(game)
    ):
        return True
    return resume_engine_in_progress(game)


def misc_phase_snap_has_healed_by(snap: object) -> bool:
    """``misc_phase_complete`` is only trusted when heal/protect maps were snapshotted."""
    norm = normalize_night_completion_snapshot(snap)
    if norm is None or not coerce_bool(norm.get("misc_phase_complete")):
        return False
    raw = norm.get("healed_by")
    return isinstance(raw, (list, tuple))


def investigative_phase_fulfilled(
    game: "Game",
    blocked: Collection[int],
    investigative_sent_actor_ids: Collection[int],
) -> bool:
    """All living investigative night actions were DM'd or roleblocked with feedback."""
    living_ids_set: Set[int] = set()
    for m in getattr(game, "living_players", []) or []:
        try:
            living_ids_set.add(int(m.id))
        except (TypeError, ValueError, AttributeError):
            continue
    blocked_set = {int(x) for x in blocked}
    sent_set = {int(x) for x in investigative_sent_actor_ids}
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    prior_sent = set(coerce_snap_id_list(snap, "investigative_sent_actor_ids"))
    for actor_id, action in list(getattr(game, "night_actions", {}).items() or []):
        if living_ids_set and int(actor_id) not in living_ids_set:
            continue
        a_type = action.get("type")
        if a_type not in _INVESTIGATIVE_ACTION_TYPES:
            continue
        if a_type == "watch" and game.player_roles.get(actor_id) != "Lookout":
            continue
        if a_type == "track" and game.player_roles.get(actor_id) != "Tracker":
            continue
        if a_type == "gaze" and game.player_roles.get(actor_id) != "Seer":
            continue
        aid = int(actor_id)
        st = game.role_states.get(aid, {}) or {}
        if aid in sent_set or aid in prior_sent or st.get("investigative_sent_tonight"):
            continue
        return False
    return True


def snapshot_chaos_visit_targets_by_actor(game: "Game") -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for pid, st in list(getattr(game, "role_states", {}).items() or []):
        if not isinstance(st, dict):
            continue
        raw = st.get("chaos_visit_targets")
        if not isinstance(raw, list) or not raw:
            continue
        targets: List[int] = []
        for x in raw:
            try:
                targets.append(int(x))
            except (TypeError, ValueError):
                continue
        if targets:
            out[str(int(pid))] = targets
    return out


def restore_night_engine_phase_checkpoint(game: "Game") -> None:
    """Restore mid-pipeline role_state + misc maps from the persisted night snapshot."""
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    if snap is None:
        return

    raw_chaos = snap.get("chaos_visit_targets_by_actor")
    if isinstance(raw_chaos, dict):
        for pid_s, targets in raw_chaos.items():
            try:
                pid = int(pid_s)
            except (TypeError, ValueError):
                continue
            if not isinstance(targets, list):
                continue
            cleaned: List[int] = []
            for t in targets:
                try:
                    cleaned.append(int(t))
                except (TypeError, ValueError):
                    continue
            if cleaned:
                game.role_states.setdefault(pid, {})["chaos_visit_targets"] = cleaned

    for pid in coerce_snap_id_list(snap, "investigative_sent_actor_ids"):
        game.role_states.setdefault(int(pid), {})["investigative_sent_tonight"] = True

    from engine.night import restore_attacked_tonight_reasons, restore_healed_by_map

    if misc_phase_complete(game) and misc_phase_snap_has_healed_by(snap):
        game._checkpoint_healed_by_map = restore_healed_by_map(snap.get("healed_by"))  # type: ignore[attr-defined]
        raw_prot = snap.get("protected_by")
        if isinstance(raw_prot, dict):
            game._checkpoint_protected_by_map = raw_prot  # type: ignore[attr-defined]
        else:
            game._checkpoint_protected_by_map = {}  # type: ignore[attr-defined]

    if misc_phase_complete(game) or killing_phase_complete(game):
        restore_attacked_tonight_reasons(game, snap.get("attacked_reasons"))


def role_state_keys_to_clear_on_pipeline_entry(
    per_night_clear_keys: tuple[str, ...], game: "Game"
) -> tuple[str, ...]:
    keys: tuple[str, ...]
    if should_preserve_role_state_on_resume(game):
        keys = tuple(k for k in per_night_clear_keys if k not in _PRESERVE_ROLE_STATE_KEYS_ON_RESUME)
    else:
        keys = per_night_clear_keys
    if chaos_phase_complete(game):
        return keys
    for _pid, st in list(getattr(game, "role_states", {}).items() or []):
        if not isinstance(st, dict):
            continue
        if st.get("chaos_used_this_night") and st.get("chaos_visit_targets"):
            return tuple(k for k in keys if k != "chaos_visit_targets")
    return keys


async def persist_chaos_visit_targets_progress(game: "Game") -> None:
    """Persist Chaos visit targets before phase-complete (crash between roll and persist_post_chaos)."""
    _merge_snap(
        game,
        {
            "chaos_roll_in_progress": True,
            "chaos_visit_targets_by_actor": snapshot_chaos_visit_targets_by_actor(game),
        },
    )
    await game.persist_flush()


async def persist_post_chaos_phase(game: "Game") -> None:
    _merge_snap(
        game,
        {
            "chaos_phase_complete": True,
            "chaos_roll_in_progress": False,
            "chaos_visit_targets_by_actor": snapshot_chaos_visit_targets_by_actor(game),
        },
    )
    await game.persist_flush()


async def persist_post_investigative_phase(
    game: "Game",
    *,
    investigative_actor_ids: Collection[int],
    phase_complete: bool,
) -> None:
    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    prior = set(coerce_snap_id_list(snap, "investigative_sent_actor_ids"))
    merged_ids = sorted(prior | {int(x) for x in investigative_actor_ids})
    patch: Dict[str, object] = {
        "investigative_sent_actor_ids": merged_ids,
        "chaos_visit_targets_by_actor": snapshot_chaos_visit_targets_by_actor(game),
    }
    if phase_complete:
        patch["investigative_phase_complete"] = True
    _merge_snap(game, patch)
    await game.persist_flush()


async def persist_transport_control_phase(
    game: "Game",
    *,
    blocked: Collection[int],
) -> None:
    _merge_snap(
        game,
        {
            "transport_control_complete": True,
            "blocked": [int(x) for x in blocked],
        },
    )
    await game.persist_flush()


async def persist_gk_sk_witch_notify_complete(game: "Game") -> None:
    _merge_snap(game, {"gk_sk_witch_notify_complete": True})
    await game.persist_flush()


async def persist_post_killing_phase(
    game: "Game",
    *,
    deaths: Set[int],
    blocked: Collection[int],
    healed_by_map: Dict[int, int],
) -> None:
    from engine.night import snapshot_attacked_tonight_reasons, snapshot_healed_by_map

    engine_deaths = sorted(int(x) for x in deaths)
    _merge_snap(
        game,
        {
            "killing_phase_complete": True,
            "deaths": engine_deaths,
            "engine_deaths": list(engine_deaths),
            "blocked": [int(x) for x in blocked],
            "healed_by": snapshot_healed_by_map(healed_by_map),
            "attacked_reasons": snapshot_attacked_tonight_reasons(game),
        },
    )
    await game.persist_flush()


async def persist_post_misc_phase(
    game: "Game",
    *,
    healed_by_map: Dict[int, int],
    protected_by_map: Dict[int, List[Dict[str, object]]],
) -> None:
    from engine.night import snapshot_attacked_tonight_reasons, snapshot_healed_by_map

    _merge_snap(
        game,
        {
            "misc_phase_complete": True,
            "healed_by": snapshot_healed_by_map(healed_by_map),
            "protected_by": protected_by_map,
            "attacked_reasons": snapshot_attacked_tonight_reasons(game),
        },
    )
    await game.persist_flush()


async def persist_engine_complete_pending_feedback(
    game: "Game",
    *,
    deaths: Set[int],
    blocked: Collection[int],
    healed_by_map: Dict[int, int],
) -> None:
    """
    Persist combat outcomes before GM ``send_night_feedback``.

    Lets ``!resolve`` resume into post-pipeline (skip ``run_night_pipeline``) after a
    crash between killing resolution and feedback delivery.
    """
    from engine.night import snapshot_attacked_tonight_reasons, snapshot_healed_by_map

    engine_deaths = sorted(int(x) for x in deaths)
    _merge_snap(
        game,
        {
            "deaths": engine_deaths,
            "engine_deaths": list(engine_deaths),
            "blocked": [int(x) for x in blocked],
            "healed_by": snapshot_healed_by_map(healed_by_map),
            "guilty_vigs": [],
            "jester_haunts": [],
            "pre_pipeline": False,
            "post_pipeline_pending": True,
            "night_engine_completed": True,
            "night_engine_running": False,
            "night_feedback_sent": False,
            "retri_consumption_done": False,
            "attacked_reasons": snapshot_attacked_tonight_reasons(game),
        },
    )
    await game.persist_flush()

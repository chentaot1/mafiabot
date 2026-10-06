"""Night ``!resolve`` crash-resume snapshot parsing and coercion."""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from persist_schema import coerce_bool


class NightResume(NamedTuple):
    resuming: bool
    resume_post_pipeline_only: bool
    resume_engine_incomplete: bool


def coerce_snap_id_list(snap: object, key: str) -> List[int]:
    raw = (snap or {}).get(key) if isinstance(snap, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def normalize_night_completion_snapshot(snap: object) -> Optional[Dict[str, object]]:
    """Coerce boolean flags and drop invalid ID lists for resume classification."""
    if not isinstance(snap, dict):
        return None
    out: Dict[str, object] = dict(snap)
    for key in (
        "pre_pipeline",
        "post_pipeline_pending",
        "night_feedback_sent",
        "night_engine_completed",
        "night_engine_running",
        "retri_consumption_done",
        "peaceful_night_announced",
        "psychic_visions_delivered",
        "chaos_phase_complete",
        "transport_control_complete",
        "investigative_phase_complete",
        "misc_phase_complete",
        "killing_phase_complete",
        "gk_sk_witch_notify_complete",
        "chaos_roll_in_progress",
    ):
        if key in out:
            out[key] = coerce_bool(out.get(key))
    for key in (
        "deaths",
        "engine_deaths",
        "blocked",
        "guilty_vigs",
        "jester_haunts",
        "pending_jester_haunts",
        "investigative_sent_actor_ids",
    ):
        if key in out:
            out[key] = coerce_snap_id_list(out, key)
    return out


def apply_stale_night_feedback_recovery(snap: object) -> Tuple[Optional[Dict[str, object]], bool]:
    """
    If feedback was persisted without a completed engine, clear flags so resolve can
    rerun combat and redeliver night DMs. Returns (snapshot, whether flags were cleared).
    """
    norm = normalize_night_completion_snapshot(snap)
    if norm is None:
        return None, False
    if coerce_bool(norm.get("night_feedback_sent")) and not coerce_bool(
        norm.get("night_engine_completed")
    ):
        out = dict(norm)
        out["night_feedback_sent"] = False
        # Keep psychic flag if investigative already completed (avoid RNG re-roll on resume).
        if not coerce_bool(norm.get("investigative_phase_complete")):
            out["psychic_visions_delivered"] = False
        return out, True
    return norm, False


def parse_night_resume_state(
    snap: object,
    *,
    day_number: int,
    game_key: object,
) -> NightResume:
    """Classify persisted night snapshot for ``!resolve`` (under ``_night_resolve_guard``)."""
    norm = normalize_night_completion_snapshot(snap)
    if norm is None:
        return NightResume(False, False, False)
    try:
        snap_day = int(norm.get("day", -1))
    except (TypeError, ValueError):
        snap_day = -1
    snap_gk = norm.get("game_key")
    gk_ok = True
    if game_key is not None and snap_gk is not None:
        gk_ok = str(snap_gk) == str(game_key)
    elif game_key is not None and snap_gk is None:
        gk_ok = bool(norm.get("pre_pipeline")) or bool(norm.get("night_engine_running"))
    resuming = snap_day == int(day_number) and gk_ok
    if not resuming:
        return NightResume(False, False, False)

    engine_completed = bool(norm.get("night_engine_completed"))
    post_pipeline_pending = bool(norm.get("post_pipeline_pending"))
    feedback_sent = bool(norm.get("night_feedback_sent"))

    # Engine already finished — never re-enter run_night_pipeline.
    if engine_completed or post_pipeline_pending:
        return NightResume(True, True, False)

    # Stale feedback without engine completion: rerun engine and allow feedback redelivery
    # (gm clears night_feedback_sent before pipeline when this shape is detected).
    if feedback_sent and not engine_completed:
        resume_engine_incomplete = True
    else:
        resume_engine_incomplete = bool(norm.get("pre_pipeline")) or bool(
            norm.get("night_engine_running")
        )
    return NightResume(True, False, resume_engine_incomplete)


def night_kill_deaths_from_snap(snap: object) -> Set[int]:
    """Engine-only deaths for guilt filtering (legacy snaps without ``engine_deaths``)."""
    if not isinstance(snap, dict):
        return set()
    engine = coerce_snap_id_list(snap, "engine_deaths")
    if engine:
        return set(engine)
    deaths = set(coerce_snap_id_list(snap, "deaths"))
    guilt = set(coerce_snap_id_list(snap, "guilty_vigs"))
    jester = set(coerce_snap_id_list(snap, "jester_haunts"))
    return deaths - guilt - jester


def coerce_player_id_set(raw: object) -> Set[int]:
    if not isinstance(raw, (set, list, tuple)):
        return set()
    out: Set[int] = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out

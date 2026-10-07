"""Game state JSON persistence — serialize/deserialize for save_state / restart."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Set


def coerce_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(int(v))
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"true", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(v)


def coerce_opt_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def coerce_role_state_int(v: object, default: int = 0) -> int:
    """Safe int for persisted ``role_states`` counters (corrupt JSON must not crash resolve)."""
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalize_graveyard_on_load(raw: object) -> List[Dict]:
    if not isinstance(raw, list):
        return []
    out: List[Dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        pid = row.get("player_id")
        try:
            row["player_id"] = int(pid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
        out.append(row)
    return out


def _normalize_used_corpses_in_role_states(role_states: Dict[int, Dict]) -> None:
    for st in role_states.values():
        if not isinstance(st, dict):
            continue
        raw = st.get("used_corpses")
        if not isinstance(raw, list):
            continue
        normalized: List[int] = []
        for x in raw:
            try:
                normalized.append(int(x))
            except (TypeError, ValueError):
                continue
        st["used_corpses"] = normalized


def game_to_persisted(game: Any) -> Dict:
    """Snapshot a Game instance to JSON-safe dict (see Game.to_persisted)."""
    # death_announce_step is stored per-player in role_states during staged death posts.
    out: Dict = {
        "gameplay": game.gameplay,
        "guild_id": game.guild_id,
        "in_progress": game.in_progress,
        "phase": game.phase,
        "resolving": game.resolving,
        "day_number": game.day_number,
        "game_key": game.game_key,
        "started_at": game.started_at,
        "player_ids": list(game._persist_player_ids) if game._rehydrate_pending else [p.id for p in game.players],
        "living_ids": list(game._persist_living_ids) if game._rehydrate_pending else [p.id for p in game.living_players],
        "player_slots": {str(k): int(v) for k, v in game.player_slots.items()},
        "player_roles": {str(k): v for k, v in game.player_roles.items()},
        "night_actions": {str(k): v for k, v in game.night.night_actions.items()},
        "role_states": {str(k): v for k, v in game.role_states.items()},
        "doused_players": sorted(game.night.doused_players),
        "graveyard": list(game.graveyard),
        "game_channel_id": game.game_channel_id,
        "mafia_tc_id": game.mafia_tc_id,
        "day_tc_id": game.day_tc_id,
        "day_vc_id": game.day_vc_id,
        "grave_tc_id": game.grave_tc_id,
        "grave_vc_id": game.grave_vc_id,
        "alive_role_id": game.alive_role_id,
        "stand_role_id": game.stand_role_id,
        "vote_in_progress": game.tribunal_state.vote_in_progress,
        "votes_today": game.tribunal_state.votes_today,
        "bloodless_cycle_streak": int(getattr(game, "bloodless_cycle_streak", 0)),
        "deaths_this_cycle": int(getattr(game, "deaths_this_cycle", 0)),
        "locked_channel_ids": list(game.locked_channel_ids),
        "lockdown_role_id": game.lockdown_role_id,
        "tribunal_muted": game.tribunal_state.tribunal_muted,
        "tribunal_defendant_id": game.tribunal_state.tribunal_defendant_id,
        "tribunal_defense_deadline_utc": game.tribunal_state.tribunal_defense_deadline_utc,
        "tribunal_judgment_deadline_utc": game.tribunal_state.tribunal_judgment_deadline_utc,
        "tribunal_judgment_message_id": game.tribunal_state.tribunal_judgment_message_id,
        "tribunal_subphase": game.tribunal_state.tribunal_subphase,
        "tribunal_verdict_committed": game.tribunal_state.tribunal_verdict_committed,
        "tribunal_last_words_deadline_utc": game.tribunal_state.tribunal_last_words_deadline_utc,
        "tribunal_resolved_judgments": dict(game.tribunal_state.tribunal_resolved_judgments),
        "tribunal_guilty_vote_count": game.tribunal_state.tribunal_guilty_vote_count,
        "tribunal_innocent_vote_count": game.tribunal_state.tribunal_innocent_vote_count,
        "tribunal_mayor_voted": game.tribunal_state.tribunal_mayor_voted,
        "tribunal_lynch_finisher_done": game.tribunal_state.tribunal_lynch_finisher_done,
        "tribunal_last_words_open_posted": game.tribunal_state.tribunal_last_words_open_posted,
        "stats_committed": bool(getattr(game, "stats_committed", False)),
        "ending": bool(getattr(game, "ending", False)),
        "cleanup_pending": bool(getattr(game, "cleanup_pending", False)),
        "bloodless_stalemate_pending": bool(getattr(game, "bloodless_stalemate_pending", False)),
        "night_transport_swaps": list(getattr(game, "night_transport_swaps", []) or []),
        "night_death_causes": {str(k): str(v) for k, v in game.night.night_death_causes.items()},
        "night_completion_snapshot": game.night.night_completion_snapshot,
        "psychic_visions_delivered_this_night": game.night.psychic_visions_delivered_this_night,
    }
    inline_pending = getattr(game, "_pending_endgame", None)
    if not (isinstance(inline_pending, dict) and inline_pending.get("outcome")):
        try:
            from persistence import load_state

            existing = load_state(int(game.guild_id)) or {}
            inline_pending = existing.get("_pending_endgame")
        except Exception:
            inline_pending = None
    if isinstance(inline_pending, dict) and inline_pending.get("outcome"):
        out["_pending_endgame"] = dict(inline_pending)
    return deepcopy(out)


def game_from_persisted(data: Dict) -> Any:
    """Rebuild a Game from persisted JSON (see Game.from_persisted)."""
    from game import Game

    try:
        guild_id_raw = data.get("guild_id")
        guild_id = int(guild_id_raw) if guild_id_raw is not None else 0
    except (TypeError, ValueError, AttributeError):
        guild_id = 0
    g = Game(guild_id)
    g.in_progress = coerce_bool(data.get("in_progress", False))
    g.phase = data.get("phase")
    # Transient lock flags should never persist across restarts.
    g.resolving = False
    try:
        g.day_number = int(data.get("day_number", 0))
    except (TypeError, ValueError):
        g.day_number = 0
    g.game_key = data.get("game_key")
    g.started_at = data.get("started_at")
    # Members are rehydrated later.
    g.players = []
    g.living_players = []
    # Corruption tolerance: persisted dict keys may not be int-coercible.
    g.player_roles = {}
    player_roles_raw = data.get("player_roles")
    if isinstance(player_roles_raw, dict):
        for k, v in player_roles_raw.items():
            try:
                g.player_roles[int(k)] = v
            except (TypeError, ValueError):
                continue

    g.night.night_actions = {}
    night_actions_raw = data.get("night_actions")
    if isinstance(night_actions_raw, dict):
        for k, v in night_actions_raw.items():
            try:
                g.night.night_actions[int(k)] = v
            except (TypeError, ValueError):
                continue

    g.night.night_death_causes = {}
    ndc_raw = data.get("night_death_causes")
    if isinstance(ndc_raw, dict):
        for k, v in ndc_raw.items():
            try:
                g.night.night_death_causes[int(k)] = str(v)
            except (TypeError, ValueError):
                continue

    from persist_validation import (
        normalize_night_completion_snapshot_for_game,
        normalize_night_transport_swaps,
    )

    snap_raw = data.get("night_completion_snapshot")
    g.night.night_completion_snapshot = normalize_night_completion_snapshot_for_game(snap_raw)
    g.night.psychic_visions_delivered_this_night = coerce_bool(
        data.get("psychic_visions_delivered_this_night", False)
    )

    g.role_states = {}
    role_states_raw = data.get("role_states")
    if isinstance(role_states_raw, dict):
        for k, v in role_states_raw.items():
            try:
                g.role_states[int(k)] = v
            except (TypeError, ValueError):
                continue
    doused: Set[int] = set()
    for x in data.get("doused_players") or []:
        try:
            doused.add(int(x))
        except (TypeError, ValueError):
            continue
    g.night.doused_players = doused
    g.graveyard = _normalize_graveyard_on_load(data.get("graveyard"))
    _normalize_used_corpses_in_role_states(g.role_states)
    from reanimate_expand import sync_retributionist_corpse_spent_state

    for pid, role in g.player_roles.items():
        if role == "Retributionist":
            sync_retributionist_corpse_spent_state(g, int(pid))
    g.stats_committed = coerce_bool(data.get("stats_committed", False))
    g.ending = coerce_bool(data.get("ending", False))
    g.cleanup_pending = coerce_bool(data.get("cleanup_pending", False))
    g.bloodless_stalemate_pending = coerce_bool(data.get("bloodless_stalemate_pending", False))
    g.night_transport_swaps = normalize_night_transport_swaps(data.get("night_transport_swaps"))

    g.game_channel_id = coerce_opt_int(data.get("game_channel_id"))
    g.mafia_tc_id = coerce_opt_int(data.get("mafia_tc_id"))
    g.day_tc_id = coerce_opt_int(data.get("day_tc_id"))
    g.day_vc_id = coerce_opt_int(data.get("day_vc_id"))
    g.grave_tc_id = coerce_opt_int(data.get("grave_tc_id"))
    g.grave_vc_id = coerce_opt_int(data.get("grave_vc_id"))
    g.alive_role_id = coerce_opt_int(data.get("alive_role_id"))
    g.stand_role_id = coerce_opt_int(data.get("stand_role_id"))
    vip_raw = coerce_bool(data.get("vote_in_progress", False))
    has_tribunal_resume = bool(
        data.get("tribunal_defendant_id") is not None
        or data.get("tribunal_subphase")
        or data.get("tribunal_defense_deadline_utc")
        or data.get("tribunal_judgment_deadline_utc")
    )
    # Restore mid-tribunal sessions only when tribunal markers exist (B4); otherwise clear stale flags.
    g.tribunal_state.vote_in_progress = bool(vip_raw and has_tribunal_resume)
    try:
        g.tribunal_state.votes_today = max(0, int(data.get("votes_today", 0)))
    except (TypeError, ValueError):
        g.tribunal_state.votes_today = 0
    try:
        g.bloodless_cycle_streak = max(0, int(data.get("bloodless_cycle_streak", 0)))
    except (TypeError, ValueError):
        g.bloodless_cycle_streak = 0
    try:
        g.deaths_this_cycle = max(0, int(data.get("deaths_this_cycle", 0)))
    except (TypeError, ValueError):
        g.deaths_this_cycle = 0
    locked: List[int] = []
    for x in data.get("locked_channel_ids") or []:
        try:
            locked.append(int(x))
        except (TypeError, ValueError):
            continue
    g.locked_channel_ids = locked
    # Corruption tolerance: role ids may be strings in persisted JSON.
    lr = data.get("lockdown_role_id")
    try:
        g.lockdown_role_id = int(lr) if lr is not None else None
    except (TypeError, ValueError):
        g.lockdown_role_id = None
    g.tribunal_state.tribunal_muted = coerce_bool(data.get("tribunal_muted", False))
    t_def = data.get("tribunal_defendant_id")
    try:
        g.tribunal_state.tribunal_defendant_id = int(t_def) if t_def is not None else None
    except (TypeError, ValueError):
        g.tribunal_state.tribunal_defendant_id = None
    g.tribunal_state.tribunal_defense_deadline_utc = data.get("tribunal_defense_deadline_utc")
    g.tribunal_state.tribunal_judgment_deadline_utc = data.get("tribunal_judgment_deadline_utc")
    tjm = data.get("tribunal_judgment_message_id")
    try:
        g.tribunal_state.tribunal_judgment_message_id = int(tjm) if tjm is not None else None
    except (TypeError, ValueError):
        g.tribunal_state.tribunal_judgment_message_id = None
    tsp = data.get("tribunal_subphase")
    g.tribunal_state.tribunal_subphase = str(tsp) if tsp else None
    g.tribunal_state.tribunal_verdict_committed = coerce_bool(data.get("tribunal_verdict_committed", False))
    g.tribunal_state.tribunal_last_words_deadline_utc = data.get("tribunal_last_words_deadline_utc")
    trj = data.get("tribunal_resolved_judgments")
    g.tribunal_state.tribunal_resolved_judgments = dict(trj) if isinstance(trj, dict) else {}
    try:
        g.tribunal_state.tribunal_guilty_vote_count = int(data.get("tribunal_guilty_vote_count", 0))
    except (TypeError, ValueError):
        g.tribunal_state.tribunal_guilty_vote_count = 0
    try:
        g.tribunal_state.tribunal_innocent_vote_count = int(data.get("tribunal_innocent_vote_count", 0))
    except (TypeError, ValueError):
        g.tribunal_state.tribunal_innocent_vote_count = 0
    g.tribunal_state.tribunal_mayor_voted = coerce_bool(data.get("tribunal_mayor_voted", False))
    g.tribunal_state.tribunal_lynch_finisher_done = coerce_bool(data.get("tribunal_lynch_finisher_done", False))
    g.tribunal_state.tribunal_last_words_open_posted = coerce_bool(
        data.get("tribunal_last_words_open_posted", False)
    )
    g.member_display_names = {}
    g._persist_player_ids = []
    player_ids_raw = data.get("player_ids")
    if isinstance(player_ids_raw, list):
        for x in player_ids_raw:
            try:
                g._persist_player_ids.append(int(x))
            except (TypeError, ValueError):
                continue
    g._persist_living_ids = []
    living_ids_raw = data.get("living_ids")
    if isinstance(living_ids_raw, list):
        for x in living_ids_raw:
            try:
                g._persist_living_ids.append(int(x))
            except (TypeError, ValueError):
                continue

    slots_raw = data.get("player_slots") or {}
    if isinstance(slots_raw, dict) and slots_raw:
        slots: Dict[int, int] = {}
        for k, v in slots_raw.items():
            try:
                pid = int(k)
                slot = int(v)
            except (TypeError, ValueError):
                continue
            slots[pid] = slot
        g.player_slots = slots
    else:
        # Back-compat: older saves won't have stable slots; derive deterministic slots from join order if possible.
        ordered_ids: List[int] = []
        player_ids_raw2 = data.get("player_ids")
        if isinstance(player_ids_raw2, list):
            for x in player_ids_raw2:
                try:
                    ordered_ids.append(int(x))
                except (TypeError, ValueError):
                    continue
        if not ordered_ids:
            # Corruption tolerance: persisted player_roles keys may not be int-coercible.
            tmp_ids: List[int] = []
            player_roles_raw2 = data.get("player_roles")
            if isinstance(player_roles_raw2, dict):
                for k in player_roles_raw2.keys():
                    try:
                        tmp_ids.append(int(k))
                    except (TypeError, ValueError):
                        continue
            ordered_ids = sorted(tmp_ids)
        g.player_slots = {pid: i + 1 for i, pid in enumerate(ordered_ids)}

    # Restart safety: any in-progress Pirate duel cannot continue after a restart.
    # Force-finish persisted duels so `!resolve` can proceed.
    for act in g.night.night_actions.values():
        if not isinstance(act, dict):
            continue
        if act.get("type") == "plunder" and not act.get("duel_finished", False) and "duel_token" not in act:
            act["duel_finished"] = True
            act["duel_outcome_ready"] = True
            act["duel_won"] = False
    inline_pe = data.get("_pending_endgame")
    if isinstance(inline_pe, dict) and inline_pe.get("outcome"):
        g._pending_endgame = dict(inline_pe)
    from gameplay.records import load_ui, repair_duel
    g.gameplay = load_ui(data.get("gameplay"))
    for action in g.night_actions.values():
        repair_duel(action, day=g.day_number, match=g.game_key)
    # An applied outcome is resumable. A half-calculated modern outcome is discarded
    # because it never changed the canonical role model.
    if data.get("gameplay") is not None:
        g.resolving = bool(g.gameplay.get("resolution") and not g.gameplay["resolution"].get("progressed"))
    return g

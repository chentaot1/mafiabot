from __future__ import annotations

import asyncio
import random
from typing import Collection, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import discord

from config import (
    ALL_MAFIA_ROLES,
    CHAOS_EFFECT_POOL,
    CHAOS_STARTING_USES,
    CONTROL_IMMUNE_ROLES,
    PIRATE_PLUNDER_ROLEBLOCK_OVERRIDES,
    PSYCHIC_ODD_EVIL_NEUTRALS,
    ROLEBLOCK_IMMUNE_ROLES,
    SEER_FRIENDLY_EXTRA_ROLES,
    SEER_HOSTILE_NEUTRAL_ROLES,
    SEER_NEUTRAL_KILLING_ROLES,
    TOWN_ROLES,
)
from engine import combat as combat_tiers
from night_action_eligibility import (
    WITCH_NON_RETARGETABLE_ACTION_TYPES,
    bodyguard_off_self_protect_eligible,
    bodyguard_self_vest_eligible,
    chaos_may_consume_use,
    chaos_targets_valid,
    chaos_try_spend_use,
    framer_frame_eligible,
    gatekeeper_blocking_active,
    gatekeeper_may_consume_use,
    gravedigger_hide_eligible,
    mole_investigate_eligible,
    retributionist_consume_eligible,
    scary_grandma_alert_eligible,
    survivor_vest_eligible,
)

if TYPE_CHECKING:
    from game import Game

# Lookout cannot identify more than this many distinct visitors (ToS-style cap).
LOOKOUT_VISITOR_CAP = 5

# Investigator buckets (ToS-style): module-level so tests and smoke gates can import them.
# Mobster appears in bucket 1 (Loaded Guns) and bucket 9 (Protective Shield); see
# investigator_bucket_for() for the special-case resolution to bucket 9.
INVESTIGATOR_BUCKETS: List[List[str]] = [
    ["Vigilante", "Scary Grandma", "Mobster", "Deputy", "Pirate"],
    ["Gravedigger", "Retributionist"],
    ["Sheriff", "Executioner", "Gatekeeper"],
    ["Framer", "Jester", "Chaos", "Survivor"],
    ["Lookout", "Tailor", "Witch"],
    ["Escort", "Transporter", "Consort", "Hypnotist"],
    ["Doctor", "Serial Killer", "Guardian Angel"],
    ["Investigator", "Mole", "Mayor", "Psychic", "Seer", "Tracker"],
    ["Bodyguard", "Mobster", "Arsonist"],
]

# Investigator: if a role is missing from buckets (modding / skewed persistence), never return a one-element list.
INVESTIGATOR_UNKNOWN_ROLE_FALLBACK: List[str] = [
    "Investigator",
    "Sheriff",
    "Lookout",
    "Tracker",
    "Doctor",
    "Bodyguard",
    "Survivor",
    "Framer",
    "Jester",
    "Mayor",
    "Mole",
    "Mobster",
    "Arsonist",
    "Serial Killer",
    "Guardian Angel",
    "Psychic",
    "Seer",
    "Deputy",
    "Pirate",
]


def investigator_bucket_for(apparent_role: str) -> List[str]:
    """Return the Investigator result bucket for an apparent role (post frame/douse tampering)."""
    if apparent_role == "Mobster":
        return list(INVESTIGATOR_BUCKETS[8])
    for bucket in INVESTIGATOR_BUCKETS:
        if apparent_role in bucket:
            return list(bucket)
    return list(INVESTIGATOR_UNKNOWN_ROLE_FALLBACK)


# ToS transport: visitors to A and B swap houses; action rows keep submitted targets.
_TRANSPORT_VISIT_IMMUNE_ACTION_TYPES = frozenset(
    {"vest", "alert", "bg_vest", "clean", "chaos"}
)


def _coerce_int_id(v: object) -> Optional[int]:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


from per_night_state import PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR

# Cleared at start_night and at run_night_pipeline entry (crash re-run idempotency).
_PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR: Tuple[str, ...] = PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR


def _clear_stale_per_night_action_flags(game: "Game") -> None:
    """Drop stale per-night markers before pipeline (mid-resolve re-run). Chaos uses separate logic."""
    from night_engine_checkpoint import role_state_keys_to_clear_on_pipeline_entry

    keys_to_clear = role_state_keys_to_clear_on_pipeline_entry(
        _PER_NIGHT_ROLE_STATE_KEYS_TO_CLEAR, game
    )
    for st in list(getattr(game, "role_states", {}).items() or []):
        if not isinstance(st, dict):
            continue
        for key in keys_to_clear:
            st.pop(key, None)
        # Preserve queued SK counters across crash-resume (cleared in start_night only).
        from night_engine_checkpoint import gk_sk_witch_notify_complete

        if not gk_sk_witch_notify_complete(game):
            st.pop("sk_counter_kills", None)


def clear_night_transport_state(game: "Game") -> None:
    game.night_transport_swaps = []
    game._transport_pairs_seen = set()
    _invalidate_effective_visit_cache(game)


def _invalidate_effective_visit_cache(game: "Game") -> None:
    game._effective_visit_destinations_cache = None


def _restore_chaos_transport_swaps(game: "Game") -> None:
    """Re-register Chaos transport from a prior pipeline pass (idempotent double-resolve)."""
    for actor_id, st in list(getattr(game, "role_states", {}).items() or []):
        if not isinstance(st, dict) or not st.get("chaos_used_this_night"):
            continue
        raw = st.get("chaos_transport_pair")
        if not isinstance(raw, list) or len(raw) != 2:
            continue
        try:
            t1, t2 = int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            continue
        _register_transport_swap(game, t1, t2, actor_id)


def _visit_destination_immune(actor_id: int, action: Dict[str, object], game: "Game") -> bool:
    if game.player_roles.get(actor_id) == "Pirate" and action.get("type") == "plunder":
        return True
    return action.get("type") in _TRANSPORT_VISIT_IMMUNE_ACTION_TYPES


def _prune_blocked_transport_swaps(game: "Game", blocked: Collection[int]) -> None:
    """Drop visitor swaps registered by roleblocked Transporters/Chaos transport actors."""
    blocked_set = {int(x) for x in blocked}
    swaps: List[Tuple[int, int, int]] = list(getattr(game, "night_transport_swaps", []) or [])
    if not swaps or not blocked_set:
        return
    kept = [(a, b, tp_id) for a, b, tp_id in swaps if tp_id not in blocked_set]
    if len(kept) == len(swaps):
        return
    game.night_transport_swaps = kept
    game._transport_pairs_seen = {(min(a, b), max(a, b)) for a, b, _ in kept}
    _invalidate_effective_visit_cache(game)


def _rebuild_visits_blocking_after_chaos_mutations(
    game: "Game",
    *,
    max_prune_passes: int = 8,
) -> Tuple[Dict[int, List[int]], List[int]]:
    """Rebuild visit log + blocking until blocked-transport prune stabilizes.

    Chaos can inject swaps/roleblocks that interact: blocking drops swaps, which
    changes visits, which changes blocking. Loop prune passes until no swap is
    removed, then one final prune + rebuild so Lookout/Tracker/alert match combat.
    """
    import logging

    prev_blocked_sig: Optional[frozenset[int]] = None
    for _ in range(max(1, int(max_prune_passes))):
        _invalidate_effective_visit_cache(game)
        visit_log_raw = build_visit_log(game)
        blocked = resolve_blocking(game, visit_log_raw)
        swaps_before = list(getattr(game, "night_transport_swaps", []) or [])
        _prune_blocked_transport_swaps(game, blocked)
        swaps_after = list(getattr(game, "night_transport_swaps", []) or [])
        blocked_sig = frozenset(int(x) for x in blocked)
        if swaps_before == swaps_after:
            if prev_blocked_sig is not None and blocked_sig == prev_blocked_sig:
                return visit_log_raw, blocked
            prev_blocked_sig = blocked_sig
            return visit_log_raw, blocked
    logging.error(
        "visit rebuild prune did not converge (guild_id=%s day=%s); using last rebuild",
        getattr(game, "guild_id", None),
        getattr(game, "day_number", None),
    )
    _invalidate_effective_visit_cache(game)
    visit_log_raw = build_visit_log(game)
    blocked = resolve_blocking(game, visit_log_raw)
    _prune_blocked_transport_swaps(game, blocked)
    _invalidate_effective_visit_cache(game)
    visit_log_raw = build_visit_log(game)
    blocked = resolve_blocking(game, visit_log_raw)
    return visit_log_raw, blocked


def _pirate_plunder_can_roleblock_target(game: "Game", target_id: int) -> bool:
    role = game.player_roles.get(target_id)
    if role not in ROLEBLOCK_IMMUNE_ROLES:
        return True
    return role in PIRATE_PLUNDER_ROLEBLOCK_OVERRIDES


def _register_transport_swap(game: "Game", a: int, b: int, transporter_id: int) -> bool:
    if a == b:
        return False
    pair_key = (min(a, b), max(a, b))
    seen: Set[Tuple[int, int]] = getattr(game, "_transport_pairs_seen", set())
    if pair_key in seen:
        return False
    seen.add(pair_key)
    game._transport_pairs_seen = seen
    swaps: List[Tuple[int, int, int]] = getattr(game, "night_transport_swaps", [])
    swaps.append((a, b, transporter_id))
    game.night_transport_swaps = swaps
    _invalidate_effective_visit_cache(game)
    return True


def _action_initial_visit_destinations(actor_id: int, action: Dict[str, object]) -> List[int]:
    a_type = action.get("type")
    if a_type in {"vest", "alert", "bg_vest", "clean"}:
        return []
    if a_type == "ward":
        # Physical visit (living GA only — dead GA ward is astral and excluded upstream).
        tid = _coerce_int_id(action.get("target"))
        return [tid] if tid is not None else []
    if a_type == "control":
        raw = action.get("targets")
        if not isinstance(raw, list) or not raw:
            return []
        tid = _coerce_int_id(raw[0])
        return [tid] if tid is not None else []
    if a_type == "gaze":
        raw = action.get("targets")
        if not isinstance(raw, list):
            return []
        out: List[int] = []
        for x in raw[:2]:
            tid = _coerce_int_id(x)
            if tid is not None:
                out.append(tid)
        return out
    if a_type == "transport":
        raw = action.get("targets", [])
        if not isinstance(raw, list) or len(raw) != 2:
            return []
        out = []
        for x in raw:
            tid = _coerce_int_id(x)
            if tid is not None:
                out.append(tid)
        return out if len(out) == 2 and len(set(out)) == 2 and all(t > 0 for t in out) else []
    if a_type == "chaos":
        return []
    tid = _coerce_int_id(action.get("target"))
    return [tid] if tid is not None else []


def _apply_visitor_swap_to_destinations(
    destinations: Dict[int, List[int]], a: int, b: int, immune_actor_ids: Set[int]
) -> None:
    for actor_id, dests in list(destinations.items()):
        if actor_id in immune_actor_ids:
            continue
        swapped: List[int] = []
        for d in dests:
            if d == a:
                swapped.append(b)
            elif d == b:
                swapped.append(a)
            else:
                swapped.append(d)
        destinations[actor_id] = swapped


def effective_visit_destinations_map(game: "Game") -> Dict[int, List[int]]:
    cached = getattr(game, "_effective_visit_destinations_cache", None)
    if isinstance(cached, dict):
        return cached

    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    destinations: Dict[int, List[int]] = {}
    immune_actor_ids: Set[int] = set()
    for actor_id, action in list(game.night_actions.items()):
        # Dead players do not visit (graveyard GA ward is astral — shield only, no visit log).
        if living_ids_set and actor_id not in living_ids_set:
            continue
        initial = _action_initial_visit_destinations(actor_id, action)
        if not initial:
            continue
        destinations[actor_id] = list(initial)
        if _visit_destination_immune(actor_id, action, game):
            immune_actor_ids.add(actor_id)

    for a, b, _tp_id in list(getattr(game, "night_transport_swaps", []) or []):
        _apply_visitor_swap_to_destinations(destinations, a, b, immune_actor_ids)
    game._effective_visit_destinations_cache = destinations
    return destinations


def effective_visit_house_for_submitted_target(game: "Game", submitted_target: int) -> int:
    """Post-transport house visited when a night action targets ``submitted_target``."""
    tid = int(submitted_target)
    for a, b, _tp_id in list(getattr(game, "night_transport_swaps", []) or []):
        if tid == a:
            tid = b
        elif tid == b:
            tid = a
    return tid


def effective_primary_target(game: "Game", actor_id: int) -> Optional[int]:
    dests = effective_visit_destinations_map(game).get(actor_id)
    if dests:
        return dests[0]
    action = game.night_actions.get(actor_id)
    if not action:
        return None
    return _coerce_int_id(action.get("target"))


def track_followed_player_id(action: Dict[str, object]) -> Optional[int]:
    """Player id whose outgoing visits Tracker follows (not the Tracker's own visit house)."""
    return _coerce_int_id(action.get("target"))


def submitted_action_target(action: Dict[str, object]) -> Optional[int]:
    """Witch/control rewrite target from an action row (``target`` or first ``targets`` entry)."""
    tid = _coerce_int_id(action.get("target"))
    if tid is not None:
        return tid
    raw = action.get("targets")
    if isinstance(raw, list) and raw:
        return _coerce_int_id(raw[0])
    return None


def tamper_subject_for_submitted_slot(game: "Game", submitted_slot: int) -> int:
    """Role-state slot for frame/hide/douse reads when a player picks ``submitted_slot``."""
    return effective_visit_house_for_submitted_target(game, submitted_slot)


async def resolve_transports(game: "Game", guild: discord.Guild) -> None:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    for actor_id, action in sorted(game.night_actions.items(), key=lambda kv: kv[0]):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if action.get("type") != "transport":
            continue

        targets = action.get("targets")
        if not isinstance(targets, list) or len(targets) != 2:
            continue
        try:
            pA_id, pB_id = int(targets[0]), int(targets[1])
        except (TypeError, ValueError):
            continue
        if _register_transport_swap(game, pA_id, pB_id, actor_id):
            notified: Set[frozenset[int]] = getattr(game, "night_transport_dm_pairs", set())
            pair_key = frozenset({int(pA_id), int(pB_id)})
            if pair_key not in notified:
                notified = set(notified)
                notified.add(pair_key)
                game.night_transport_dm_pairs = notified
            else:
                continue
            for p_id in [pA_id, pB_id]:
                m = await game.get_member_safe(guild, p_id)
                if m:
                    try:
                        from messages import tos as tos_msg

                        await m.send(tos_msg.transported())
                    except discord.HTTPException:
                        pass


async def resolve_control(game: "Game", guild: discord.Guild) -> None:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    pending_actions = []

    # Pre-compute Gatekeeper-block analysis using the same chain-aware logic as
    # resolve_blocking(). The Witch's "visit" to her controlled target IS the
    # control attempt, so if that visit would be Gatekeeper-blocked (with all
    # chain semantics — roleblocked-blockers, Gatekeeper-blocked-roleblockers,
    # etc.) the control must fail. We only compute this if any control action
    # exists to avoid unnecessary work.
    gk_blocked_pre: Set[int] = set()
    if any(a.get("type") == "control" for a in game.night_actions.values()):
        pre_visit_log = build_visit_log(game)
        gk_blocked_pre, _rb_blocked_pre = _compute_blocked_sets(game, pre_visit_log, living_ids_set)

    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if action.get("type") != "control":
            continue

        targets = action.get("targets")
        if not isinstance(targets, list) or len(targets) != 2:
            # Corrupted/invalid persisted state safety: ignore malformed control actions.
            continue
        try:
            controlled_id, final_target_id = int(targets[0]), int(targets[1])
        except (TypeError, ValueError):
            continue
        psychic_theft_ok = False
        actor = await game.get_member_safe(guild, actor_id)
        controlled = await game.get_member_safe(guild, controlled_id)
        final_tgt = await game.get_member_safe(guild, final_target_id)

        if game.player_roles.get(controlled_id) in CONTROL_IMMUNE_ROLES:
            from messages import tos as tos_msg

            await _dm_actor_id(game, guild, actor_id, tos_msg.witch_control_resisted())
            continue

        # If the Witch herself would be Gatekeeper-blocked (visiting a guarded
        # target), the control attempt fails. This matches resolve_blocking()'s
        # final outcome and correctly handles chained block scenarios.
        if actor_id in gk_blocked_pre:
            continue

        if controlled:
            from messages import tos as tos_msg

            await _dm_player(controlled, tos_msg.witch_controlled())

        # If the controlled player did not submit an action, Witch can still force certain roles
        # to act (ToS-like). Keep this narrow to avoid phantom-visit side effects.
        if controlled_id not in game.night_actions:
            forced = False
            controlled_role = game.player_roles.get(controlled_id)
            controlled_state = game.role_states.get(controlled_id, {})

            if controlled_role == "Vigilante":
                if final_target_id == controlled_id:
                    forced = False
                elif (
                    controlled_state.get("shots_remaining", 0) > 0
                    and not controlled_state.get("will_die_of_guilt")
                    and not controlled_state.get("guilty_tomorrow")
                ):
                    pending_actions.append(
                        (
                            controlled_id,
                            {"type": "shoot", "target": final_target_id, "actor": controlled_id, "forced_by_witch": True},
                        )
                    )
                    forced = True

            elif controlled_role == "Psychic":
                # Passive vision theft (no night action row). Suppress "no redirectable action".
                game.role_states.setdefault(controlled_id, {})["psychic_vision_recipient_id"] = actor_id
                psychic_theft_ok = True
                if controlled:
                    ctrl_name = controlled.display_name
                    await _dm_actor_id(
                        game, guild, actor_id, tos_msg.witch_psychic_bent(ctrl_name)
                    )

            elif controlled_role == "Seer":
                # Idle Seer: Witch's forced target fills both gaze slots (self-pair → Friends).
                pending_actions.append(
                    (
                        controlled_id,
                        {
                            "type": "gaze",
                            "targets": [final_target_id, final_target_id],
                            "actor": controlled_id,
                            "forced_by_witch": True,
                            "_controlled_by": actor_id,
                        },
                    )
                )
                forced = True

            if forced:
                if controlled and final_tgt:
                    await _dm_actor_id(
                        game,
                        guild,
                        actor_id,
                        tos_msg.witch_forced_target(
                            controlled.display_name, final_tgt.display_name
                        ),
                    )
                continue

        redirected = False
        for act_actor_id, act in list(game.night_actions.items()):
            if act_actor_id == controlled_id and act.get("type") != "control":
                # ToS-like: Survivor vest always targets self; Witch cannot retarget it.
                if act.get("type") in WITCH_NON_RETARGETABLE_ACTION_TYPES:
                    from messages import tos as tos_msg

                    await _dm_actor_id(
                        game, guild, actor_id, tos_msg.witch_cannot_redirect_self_target()
                    )
                    redirected = True
                    break
                if act.get("type") == "protect" and int(final_target_id) == int(controlled_id):
                    from messages import tos as tos_msg

                    await _dm_actor_id(
                        game, guild, actor_id, tos_msg.witch_cannot_redirect_self_target()
                    )
                    redirected = True
                    break
                # Arsonist: Witch can prevent ignite by forcing a douse instead.
                # (Ignite has no target to redirect.)
                if act.get("type") == "ignite" and game.player_roles.get(controlled_id) == "Arsonist":
                    pending_actions.append(
                        (
                            controlled_id,
                            {"type": "douse", "target": final_target_id, "actor": controlled_id, "forced_by_witch": True},
                        )
                    )
                    if controlled and final_tgt:
                        await _dm_actor_id(
                            game,
                            guild,
                            actor_id,
                            tos_msg.witch_prevented_ignite(
                                controlled.display_name, final_tgt.display_name
                            ),
                        )
                    redirected = True
                    break
                if "target" in act:
                    act["target"] = final_target_id
                if "targets" in act:
                    # Multi-target: Witch's second control parameter is the forced victim.
                    # (Transporter is control-immune in this ruleset, but keep this safe anyway.)
                    tg = act["targets"]
                    if isinstance(tg, list) and tg:
                        if act.get("type") == "gaze" and len(tg) >= 2:
                            try:
                                orig0, orig1 = int(tg[0]), int(tg[1])
                            except (TypeError, ValueError):
                                orig0, orig1 = None, None
                            if orig0 is not None and orig1 is not None:
                                tg[0] = final_target_id
                                # Avoid Seer self-pair (a==b) if forcing collides with the untouched slot.
                                if int(final_target_id) == orig1:
                                    tg[1] = orig0
                                else:
                                    tg[1] = orig1
                        else:
                            tg[0] = final_target_id
                # ToS-like: Witch receives the *results* the controlled target would have gotten.
                # Tag the controlled action so downstream investigative/watch resolution can mirror the DM.
                act["_controlled_by"] = actor_id
                redirected = True
                break

        if not redirected and not psychic_theft_ok and controlled:
            await _dm_actor_id(
                game,
                guild,
                actor_id,
                tos_msg.witch_no_redirectable_action(controlled.display_name),
            )

        real_role = game.player_roles.get(controlled_id, "Unknown")
        skip_consig = any(
            aid == controlled_id
            and act.get("type") == "investigate"
            and game.player_roles.get(controlled_id) == "Mole"
            for aid, act in game.night_actions.items()
        )
        if not skip_consig:
            from messages.role_catalog import consig_blurb

            await _dm_actor_id(game, guild, actor_id, consig_blurb(real_role))

    # Apply pending actions safely after iterating
    for p_id, payload in pending_actions:
        if p_id in game.night_actions:
            game.night_actions[p_id].update(payload)
        else:
            game.night_actions[p_id] = payload
    _invalidate_effective_visit_cache(game)


async def finalize_witch_control_feedback(game: "Game", guild: discord.Guild, blocked: List[int]) -> None:
    """After final blocking pass: confirm or revoke control tags and notify the Witch."""
    from messages import tos as tos_msg

    blocked_set = {int(b) for b in blocked}
    for actor_id, action in list(game.night_actions.items()):
        ctrl = action.get("_controlled_by")
        if ctrl is None:
            continue
        try:
            wid = int(ctrl)
        except (TypeError, ValueError):
            continue
        witch = await game.get_member_safe(guild, wid)
        controlled = await game.get_member_safe(guild, actor_id)
        if actor_id in blocked_set or wid in blocked_set:
            action.pop("_controlled_by", None)
            if witch:
                try:
                    await witch.send(tos_msg.witch_control_pawn_blocked())
                except discord.HTTPException:
                    pass
            continue
        if witch and controlled:
            forced_id = submitted_action_target(action)
            final_tgt = await game.get_member_safe(guild, forced_id) if forced_id is not None else None
            if final_tgt:
                try:
                    await witch.send(
                        tos_msg.witch_forced_target(
                            controlled.display_name, final_tgt.display_name
                        )
                    )
                except discord.HTTPException:
                    pass


def build_visit_log(game: "Game") -> Dict[int, List[int]]:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    visit_log: Dict[int, List[int]] = {}
    for actor_id, dests in effective_visit_destinations_map(game).items():
        if living_ids_set and actor_id not in living_ids_set:
            continue
        action = game.night_actions.get(actor_id)
        # Retributionist corpse abilities visit via append_retributionist_corpse_visits only.
        if isinstance(action, dict) and action.get("_from_retri") is not None:
            continue
        for tid in dests:
            # Allow duplicate entries (e.g. Witch-forced Seer gaze [T, T] → Lookout sees twice).
            visit_log.setdefault(tid, []).append(actor_id)

    # Chaos always visits both !chaos targets once the effect resolves (even if the
    # rolled effect replaces the action with roleblock / watch / etc.).
    for actor_id, st in list(getattr(game, "role_states", {}).items() or {}):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if not isinstance(st, dict):
            continue
        raw = st.get("chaos_visit_targets")
        if not isinstance(raw, list):
            continue
        for t_id in raw:
            tid = _coerce_int_id(t_id)
            if tid is None:
                continue
            visitors = visit_log.setdefault(tid, [])
            if actor_id not in visitors:
                visitors.append(actor_id)

    from reanimate_expand import append_retributionist_corpse_visits

    append_retributionist_corpse_visits(game, visit_log)
    return visit_log


def _compute_blocked_sets(
    game: "Game",
    visit_log: Dict[int, List[int]],
    living_ids_set: Set[int],
) -> Tuple[Set[int], Set[int]]:
    """Return (gatekeeper_blocked, roleblock_blocked) with no side effects.

    Mirrors resolve_blocking()'s chain-aware fixed point so other phases of the
    pipeline (e.g. resolve_control()) can ask "is X effectively blocked?" using
    the exact same semantics. Honors `_from_chaos` guards as if a Gatekeeper had
    issued them, so Chaos-injected guards block visitors like a real guard.
    """
    blockers: List[Tuple[int, int, str]] = []
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        a_type = action.get("type")
        if a_type in {"roleblock", "plunder"}:
            tid = _roleblock_plunder_effect_target(game, int(actor_id), action)
            if tid is None:
                continue
            blockers.append((actor_id, tid, str(a_type)))

    roleblock_blocked: Set[int] = set()
    gatekeeper_blocked: Set[int] = set()
    outer_seen: Set[tuple[frozenset[int], frozenset[int]]] = set()

    def _gatekeeper_blocks(exclude_visitors: Set[int]) -> Set[int]:
        out: Set[int] = set()
        for actor_id, action in list(game.night_actions.items()):
            if living_ids_set and actor_id not in living_ids_set:
                continue
            if action.get("type") != "guard":
                continue
            # If the guard actor is blocked (roleblock/plunder/other), guard doesn't apply.
            if actor_id in exclude_visitors:
                continue
            # Allow real Gatekeepers and Chaos-injected guards.
            if game.player_roles.get(actor_id) != "Gatekeeper" and not action.get("_from_chaos"):
                continue
            target_id = effective_primary_target(game, actor_id)
            if not gatekeeper_blocking_active(
                game,
                actor_id,
                action,
                effective_target_id=target_id,
                back_to_back_rejects=gatekeeper_back_to_back_rejects,
            ):
                continue

            for visitor_id in (v for v in visit_log.get(target_id, []) if v not in exclude_visitors):
                # Never block the guard actor on its own guard (matters for
                # Chaos-injected guards where Chaos also "visits" the target).
                if visitor_id == actor_id:
                    continue
                visitor_role = game.player_roles.get(visitor_id)
                if visitor_role not in ALL_MAFIA_ROLES and visitor_role != "Transporter":
                    out.add(visitor_id)
        return out

    def _roleblock_fixed_point(blocked_actors: Set[int]) -> Set[int]:
        blocked_set_local: Set[int] = set(blocked_actors)
        seen_local: Set[frozenset[int]] = set()
        while True:
            new_targets: Set[int] = set()
            for actor_id, target_id, a_type in blockers:
                if actor_id in blocked_set_local:
                    continue
                target_role = game.player_roles.get(target_id)
                if target_role in ROLEBLOCK_IMMUNE_ROLES:
                    if a_type == "plunder" and _pirate_plunder_can_roleblock_target(game, target_id):
                        pass
                    else:
                        continue
                new_targets.add(target_id)
            new_blocked = set(blocked_actors) | new_targets
            key = frozenset(new_blocked)
            if key in seen_local:
                blocked_set_local = new_blocked
                break
            seen_local.add(key)
            if new_blocked == blocked_set_local:
                blocked_set_local = new_blocked
                break
            blocked_set_local = new_blocked
        return blocked_set_local - set(blocked_actors)

    for _ in range(12):
        gatekeeper_blocked = _gatekeeper_blocks(roleblock_blocked)
        roleblock_blocked_next = _roleblock_fixed_point(gatekeeper_blocked)
        key = (frozenset(gatekeeper_blocked), frozenset(roleblock_blocked_next))
        if key in outer_seen:
            roleblock_blocked = roleblock_blocked_next
            break
        outer_seen.add(key)
        if roleblock_blocked_next == roleblock_blocked:
            roleblock_blocked = roleblock_blocked_next
            break
        roleblock_blocked = roleblock_blocked_next

    return gatekeeper_blocked, roleblock_blocked


def resolve_blocking(game: "Game", visit_log: Dict[int, List[int]]) -> List[int]:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]

    gatekeeper_blocked, roleblock_blocked = _compute_blocked_sets(game, visit_log, living_ids_set)
    blocked_set: Set[int] = set(gatekeeper_blocked) | set(roleblock_blocked)

    # Consume Gatekeeper uses exactly once per active guard (well-formed, Gatekeeper not blocked).
    # Chaos-injected guards (`_from_chaos`) do NOT consume Gatekeeper uses.
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if action.get("type") != "guard":
            continue
        if game.player_roles.get(actor_id) != "Gatekeeper":
            continue
        if actor_id in blocked_set:
            continue
        st = game.role_states.get(actor_id, {})
        if st.get("gatekeeper_used_this_night"):
            continue
        eff_guard_tid = effective_primary_target(game, actor_id)
        if not gatekeeper_may_consume_use(
            game,
            actor_id,
            action,
            effective_target_id=eff_guard_tid,
            back_to_back_rejects=gatekeeper_back_to_back_rejects,
        ):
            continue
        if "uses_remaining" in st:
            st["uses_remaining"] = max(0, int(st.get("uses_remaining", 0)) - 1)
            st["gatekeeper_used_this_night"] = True
            try:
                st["gatekeeper_last_guard_target_id"] = int(eff_guard_tid)
                st["gatekeeper_last_successful_guard_day_number"] = int(getattr(game, "day_number", 0))
            except (TypeError, ValueError):
                pass

    # Mark Gatekeeper blocks for feedback.
    for p_id in gatekeeper_blocked:
        game.night_actions.setdefault(p_id, {})["blocked_by_gatekeeper"] = True
    for p_id in roleblock_blocked:
        game.night_actions.setdefault(p_id, {})["blocked_by_roleblock"] = True

    return list(blocked_set)


async def notify_gatekeeper_blocked_visitors(
    game: "Game", guild: discord.Guild, visit_log: Dict[int, List[int]], gatekeeper_blocked: Set[int]
) -> None:
    """DM visitors turned away by a Gatekeeper guard (Spec 4c)."""
    from messages import tos as tos_msg
    from messages.delivery import dm_member

    living_ids_set: Set[int] = {
        int(m.id) for m in getattr(game, "living_players", []) or []
    }  # type: ignore[union-attr]
    for visitor_id in gatekeeper_blocked:
        if living_ids_set and visitor_id not in living_ids_set:
            continue
        member = await game.get_member_safe(guild, visitor_id)
        if member:
            await dm_member(member, tos_msg.gatekeeper_turned_away())
    for gk_id, action in list(game.night_actions.items()):
        if action.get("type") != "guard":
            continue
        if game.player_roles.get(gk_id) != "Gatekeeper":
            continue
        guard_tid = effective_primary_target(game, gk_id)
        if guard_tid is None:
            continue
        blocked_visitors = [
            v
            for v in visit_log.get(guard_tid, [])
            if v in gatekeeper_blocked and v != gk_id
        ]
        if not blocked_visitors:
            continue
        gk_member = await game.get_member_safe(guild, gk_id)
        if gk_member:
            await dm_member(gk_member, tos_msg.gatekeeper_blocked_visitor())


def gatekeeper_guard_effective_target(
    game: "Game", gk_id: int, submitted_target_id: int
) -> int:
    """Guarded player id after registered transport swaps, else the submitted slot."""
    prior = game.night_actions.get(gk_id)
    game.night_actions[gk_id] = {
        "type": "guard",
        "target": int(submitted_target_id),
        "actor": int(gk_id),
    }
    _invalidate_effective_visit_cache(game)
    try:
        eff = effective_primary_target(game, int(gk_id))
        return int(eff) if eff is not None else int(submitted_target_id)
    finally:
        if prior is None:
            game.night_actions.pop(gk_id, None)
        else:
            game.night_actions[gk_id] = prior
        _invalidate_effective_visit_cache(game)


def gatekeeper_back_to_back_rejects(
    game: "Game", gk_id: int, submitted_target_id: int
) -> bool:
    """True when a guard would break back-to-back rule (transport-aware)."""
    last_tid = game.role_states.get(gk_id, {}).get("gatekeeper_last_guard_target_id")
    last_day = game.role_states.get(gk_id, {}).get(
        "gatekeeper_last_successful_guard_day_number"
    )
    if last_tid is None or last_day is None:
        return False
    if int(getattr(game, "day_number", 0)) != int(last_day) + 1:
        return False
    eff = gatekeeper_guard_effective_target(game, int(gk_id), int(submitted_target_id))
    return int(eff) == int(last_tid)


def _roleblock_plunder_effect_target(
    game: "Game", actor_id: int, action: Dict[str, object]
) -> Optional[int]:
    """Post-transport slot for roleblock; plunder uses submitted target (Pirate visit is TP-immune)."""
    if action.get("type") == "plunder":
        return _coerce_int_id(action.get("target"))
    tid = effective_primary_target(game, actor_id)
    if tid is not None:
        return tid
    return _coerce_int_id(action.get("target"))


def _sk_roleblock_counter_victim(
    game: "Game", actor_id: int, action: Dict[str, object]
) -> Optional[int]:
    """Who the aggressive SK stabs after a roleblock attempt on them (ToS-style)."""
    role = game.player_roles.get(actor_id)
    if role in ("Escort", "Consort", "Chaos"):
        return int(actor_id)
    if role == "Retributionist" and action.get("_from_retri") is not None:
        from reanimate_expand import graveyard_real_role_for_corpse

        if graveyard_real_role_for_corpse(game, action.get("_from_retri")) in ("Escort", "Consort"):
            return int(actor_id)
    return None


async def serial_killer_escort_counters(game: "Game", guild: discord.Guild, blocked: List[int]) -> None:
    """Roleblock on SK (Escort/Consort/Chaos/Retri Escort corpse): immune DM + optional counter on the actor."""
    blocked_set = set(blocked)
    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "roleblock":
            continue
        tgt = _roleblock_plunder_effect_target(game, int(actor_id), action)
        if tgt is None:
            continue
        if game.player_roles.get(tgt) != "Serial Killer":
            continue
        counter_victim = _sk_roleblock_counter_victim(game, int(actor_id), action)
        if counter_victim is None:
            continue
        if counter_victim in blocked_set:
            continue
        sk_mem = await game.get_member_safe(guild, tgt)
        if sk_mem:
            from messages import tos as tos_msg

            try:
                await sk_mem.send(tos_msg.sk_roleblock_immune())
            except discord.HTTPException:
                pass
        st = game.role_states.setdefault(tgt, {})
        if st.get("sk_cautious"):
            continue
        li = st.setdefault("sk_counter_kills", [])
        if counter_victim not in li:
            li.append(counter_victim)


async def apply_misc_actions(
    game: "Game", blocked: List[int], guild: discord.Guild
) -> Tuple[Dict[int, int], Dict[int, List[Dict[str, object]]]]:
    from night_engine_checkpoint import misc_phase_complete, misc_phase_snap_has_healed_by

    snap = getattr(game, "night_completion_snapshot", None)
    if misc_phase_complete(game) and misc_phase_snap_has_healed_by(snap):
        healed = getattr(game, "_checkpoint_healed_by_map", None)
        protected = getattr(game, "_checkpoint_protected_by_map", None)
        if isinstance(healed, dict) and isinstance(protected, dict):
            return dict(healed), dict(protected)  # type: ignore[return-value]

    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    # Multi-target support: multiple Doctors/Bodyguards can act in the same night.
    # healed_by_map: target_id -> healer_id
    # protected_by_map: target_id -> [bodyguard_ids...]
    healed_by_map: Dict[int, int] = {}
    # protected_by_map: target_id -> list of protectors
    # protector entries are dicts: {"id": int, "dies_on_guard": bool}
    protected_by_map: Dict[int, List[Dict[str, object]]] = {}

    def _coerce_int(v: object) -> Optional[int]:
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    # Guardian Angel ward (dead GA may still resolve ward).
    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "ward":
            continue
        if game.player_roles.get(actor_id) != "Guardian Angel":
            continue
        tgt = _coerce_int(action.get("target"))
        bind_expect = game.role_states.get(actor_id, {}).get("ga_target_id")
        try:
            bind_expect_int = int(bind_expect) if bind_expect is not None else None
        except (TypeError, ValueError):
            bind_expect_int = None
        if tgt is None or bind_expect_int is None or tgt != bind_expect_int:
            continue
        st_ga = game.role_states.setdefault(actor_id, {})
        if bool(st_ga.get("ga_defeated")):
            continue
        ga_alive = actor_id in living_ids_set
        if ga_alive and actor_id in blocked:
            ga_m = await game.get_member_safe(guild, actor_id)
            if ga_m:
                from messages import tos as tos_msg

                try:
                    await ga_m.send(tos_msg.ga_ward_rb_no_charge())
                except discord.HTTPException:
                    pass
            continue
        if int(st_ga.get("ga_ward_charges", 0)) <= 0:
            ga_m = await game.get_member_safe(guild, actor_id)
            if ga_m:
                from messages import tos as tos_msg

                try:
                    await ga_m.send(tos_msg.ga_ward_no_charge())
                except discord.HTTPException:
                    pass
            continue
        st_ga["ga_ward_charges"] = 0
        st_bind = game.role_states.setdefault(tgt, {})
        st_bind["ga_shield_active_tonight"] = True
        try:
            st_bind["ga_trial_lock_day"] = int(getattr(game, "day_number", 0)) + 1
        except (TypeError, ValueError):
            st_bind["ga_trial_lock_day"] = 1
        game.doused_players.discard(tgt)
        st_ga["ga_announce_pending"] = True
        ga_m = await game.get_member_safe(guild, actor_id)
        bind_m = await game.get_member_safe(guild, tgt)
        if ga_m:
            from messages import tos as tos_msg

            try:
                await ga_m.send(tos_msg.ga_ward_applied())
            except discord.HTTPException:
                pass
        if bind_m:
            from messages import tos as tos_msg

            try:
                await bind_m.send(tos_msg.ga_ward_received())
            except discord.HTTPException:
                pass

    # Deterministic priority: apply frames before other misc effects
    # so investigative outcomes don't depend on dict iteration order.
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if actor_id in blocked:
            continue
        if action.get("type") == "frame":
            if game.player_roles.get(actor_id) == "Framer" and not framer_frame_eligible(game):
                continue
            tgt = effective_primary_target(game, actor_id)
            if tgt is None:
                continue
            game.role_states.setdefault(tgt, {})["is_framed"] = True

    for actor_id, action in sorted(game.night_actions.items(), key=lambda kv: kv[0]):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        a_type = action.get("type")
        if actor_id in blocked:
            if a_type == "protect" and game.player_roles.get(actor_id) == "Bodyguard":
                bg_m = await game.get_member_safe(guild, actor_id)
                if bg_m:
                    from messages import tos as tos_msg

                    try:
                        await _dm_player(bg_m, tos_msg.bodyguard_rb_no_protect())
                    except discord.HTTPException:
                        pass
            continue

        if a_type == "heal":
            target_id = effective_primary_target(game, actor_id)
            if target_id is None:
                continue
            # House rule: revealed Mayor cannot be healed (including Retributionist Doctor corpse).
            if game.role_states.get(target_id, {}).get("is_revealed") and game.player_roles.get(target_id) == "Mayor":
                continue
            # Self-heal cap: Doctor (or Retributionist Doctor-corpse on self) cannot bypass cap.
            if target_id == actor_id:
                cap_holder = actor_id
                raw_corpse = action.get("_from_retri")
                if raw_corpse is not None:
                    try:
                        cap_holder = int(raw_corpse)
                    except (TypeError, ValueError):
                        cap_holder = actor_id
                state = game.role_states.get(cap_holder, {})
                if "self_heals_remaining" in state and int(state.get("self_heals_remaining", 0)) <= 0:
                    continue
            if target_id in healed_by_map:
                from messages import tos as tos_msg

                healer = await game.get_member_safe(guild, actor_id)
                if healer:
                    role = game.player_roles.get(actor_id)
                    if role == "Doctor":
                        await _dm_player(healer, tos_msg.doctor_heal_redundant())
                    elif role == "Retributionist" and action.get("_from_retri") is not None:
                        await _dm_player(healer, tos_msg.doctor_heal_redundant())
                continue
            healed_by_map[target_id] = actor_id
            if target_id == actor_id:
                cap_holder = actor_id
                raw_corpse = action.get("_from_retri")
                if raw_corpse is not None:
                    try:
                        cap_holder = int(raw_corpse)
                    except (TypeError, ValueError):
                        cap_holder = actor_id
                state = game.role_states.get(cap_holder, {})
                if "self_heals_remaining" in state and not state.get("self_heal_used_this_night"):
                    state["self_heals_remaining"] = max(0, int(state.get("self_heals_remaining", 0)) - 1)
                    state["self_heal_used_this_night"] = True

        elif a_type == "protect":
            protected_target = effective_primary_target(game, actor_id)
            if protected_target is None:
                continue
            state = game.role_states.get(actor_id, {})
            if protected_target == actor_id:
                # Command routes self → bg_vest; engine ignores corrupt protect-on-self rows.
                continue
            if not bodyguard_off_self_protect_eligible(game, actor_id):
                continue
            protected_by_map.setdefault(protected_target, []).append({"id": actor_id, "dies_on_guard": True})
            if "uses_remaining" in state and not state.get("bg_protect_used_this_night"):
                state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                state["bg_protect_used_this_night"] = True
        elif a_type == "ret_protect":
            if not retributionist_consume_eligible(game, actor_id):
                continue
            protected_target = effective_primary_target(game, actor_id)
            if protected_target is None:
                continue
            raw_corpse = action.get("_from_retri")
            if raw_corpse is None:
                continue
            try:
                corpse_guard_id = int(raw_corpse)
            except (TypeError, ValueError):
                continue
            # Corpse Bodyguard performs the guard; living Retributionist does not die on guard.
            protected_by_map.setdefault(protected_target, []).append(
                {
                    "id": corpse_guard_id,
                    "dies_on_guard": True,
                    "retri_actor_id": actor_id,
                }
            )
        elif a_type == "bg_vest":
            # ToS-like Bodyguard self-protect: a one-time vest (no counterattack)
            state = game.role_states.get(actor_id, {})
            if not bodyguard_self_vest_eligible(game, actor_id):
                continue
            game.role_states.setdefault(actor_id, {})["is_vested"] = True
            if "self_protects_remaining" in state and not state.get("bg_self_protect_used_this_night"):
                state["self_protects_remaining"] = max(0, int(state.get("self_protects_remaining", 0)) - 1)
                state["bg_self_protect_used_this_night"] = True

        elif a_type == "vest":
            if not survivor_vest_eligible(game, actor_id):
                continue
            state = game.role_states.get(actor_id, {})
            game.role_states.setdefault(actor_id, {})["is_vested"] = True
            if "vests_remaining" in state and not state.get("vest_used_this_night"):
                state["vests_remaining"] = max(0, int(state.get("vests_remaining", 0)) - 1)
                state["vest_used_this_night"] = True

        elif a_type == "alert":
            if not scary_grandma_alert_eligible(game, actor_id):
                continue
            state = game.role_states.get(actor_id, {})
            game.role_states.setdefault(actor_id, {})["is_on_alert"] = True
            if "alerts_remaining" in state and not state.get("alert_used_this_night"):
                state["alerts_remaining"] = max(0, int(state.get("alerts_remaining", 0)) - 1)
                state["alert_used_this_night"] = True

        elif a_type == "tailor":
            tgt = effective_primary_target(game, actor_id)
            if tgt is None:
                continue
            fake_role = action.get("fake_role")
            if not isinstance(fake_role, str) or not fake_role:
                continue
            state = game.role_states.get(actor_id, {})
            # Corrupted/persisted action safety: if the use count is 0, treat as inert
            # (mirrors the vest / alert / bg_vest guards) — audit #9.
            if int(state.get("uses_remaining", 0)) <= 0:
                continue
            game.role_states.setdefault(tgt, {})["is_tailored_as"] = fake_role
            if "uses_remaining" in state and not state.get("tailor_used_this_night"):
                state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                state["tailor_used_this_night"] = True

        elif a_type == "hide":
            if not gravedigger_hide_eligible(game, actor_id):
                continue
            tgt = effective_primary_target(game, actor_id)
            if tgt is None:
                continue
            game.role_states.setdefault(tgt, {})["is_hidden_by_gravedigger"] = True
            state = game.role_states.get(actor_id, {})
            if "uses_remaining" in state and not state.get("gravedigger_used_this_night"):
                state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                state["gravedigger_used_this_night"] = True

        elif a_type == "douse":
            target_id = effective_primary_target(game, actor_id)
            if target_id is None:
                continue
            # Mirror the killing-branch living-ids guard: a stale or transport-
            # redirected douse pointing at a non-living id should be inert
            # (audit #17).
            if living_ids_set and target_id not in living_ids_set:
                continue
            if target_id not in game.doused_players:
                game.doused_players.add(target_id)
                target_member = await game.get_member_safe(guild, target_id)
                if target_member:
                    try:
                        from messages import tos as tos_msg

                        await target_member.send(tos_msg.arso_smell_gasoline())
                    except discord.HTTPException:
                        pass
        elif a_type == "clean":
            # Applied in a second pass so `clean` always wins against gasoline applied the same night.
            continue

    # Second pass: Arsonist clean (always after douses for the night are registered).
    for actor_id, action in list(game.night_actions.items()):
        if actor_id in blocked:
            continue
        if action.get("type") != "clean":
            continue
        if actor_id in game.doused_players:
            game.doused_players.remove(actor_id)

    return healed_by_map, protected_by_map


async def _dm_player(member: Optional[discord.Member], text: str) -> None:
    if not member:
        return
    try:
        await member.send(text)
    except discord.HTTPException:
        pass


async def _dm_actor_id(game: "Game", guild: discord.Guild, actor_id: int, text: str) -> bool:
    """Deliver a night result DM; enqueue outbox if the member cannot be resolved."""
    member = await game.get_member_safe(guild, actor_id)
    if member:
        await _dm_player(member, text)
        return True
    from game import try_get_bot

    bot = try_get_bot()
    db = getattr(bot, "db", None) if bot is not None else None
    if db is None:
        return False
    gk = getattr(game, "game_key", None) or "unknown"
    digest = abs(hash(text)) % (10**12)
    db.enqueue_dm_outbox(
        guild_id=int(game.guild_id),
        kind="night_result",
        dedupe_key=f"mafia_night:{game.guild_id}:{gk}:{int(game.day_number)}:{int(actor_id)}:{digest}",
        target_user_id=int(actor_id),
        content=text,
    )
    return True


def _lookout_visitors_excluding_self(visitors: List[int], watcher_id: int) -> List[int]:
    wid = int(watcher_id)
    return [int(v) for v in visitors if int(v) != wid]


async def _mirror_feedback_to_witch(
    game: "Game", guild: discord.Guild, feedback: str, controller_id: object
) -> None:
    try:
        wid = int(controller_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return
    await _dm_actor_id(game, guild, wid, feedback)


def _investigative_sent_tonight(game: "Game", actor_id: int) -> bool:
    return bool((game.role_states.get(actor_id) or {}).get("investigative_sent_tonight"))


def _mark_investigative_sent_tonight(game: "Game", actor_id: int) -> None:
    game.role_states.setdefault(actor_id, {})["investigative_sent_tonight"] = True


async def _investigative_target_display_name(
    game: "Game", guild: discord.Guild, target_id: int
) -> str:
    from messages import tos as tos_msg

    member = await game.get_member_safe(guild, target_id)
    if member:
        return member.display_name
    return tos_msg.format_player(game, int(target_id))


async def _visitor_display_name(game: "Game", guild: discord.Guild, player_id: int) -> str:
    """Display name for a player slot (Lookout visitors or Tracker visit destinations)."""
    member = await game.get_member_safe(guild, player_id)
    if member:
        return member.display_name
    from messages import tos as tos_msg

    for entry in getattr(game, "graveyard", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("player_id")) == int(player_id):
                slot = game.player_slots.get(int(player_id), "?")
                role = entry.get("real_role") or "?"
                return f"Slot {slot} ({role} corpse)"
        except (TypeError, ValueError):
            continue
    return tos_msg.format_player(game, int(player_id))


async def resolve_investigative(
    game: "Game", blocked: List[int], visit_log: Dict[int, List[int]], guild: discord.Guild
) -> None:
    """Resolve investigate/watch/track/gaze. Role checks intentionally skipped for Chaos/Retri injects."""
    from messages import tos as tos_msg
    from night_engine_checkpoint import (
        investigative_phase_complete,
        investigative_phase_fulfilled,
        persist_post_investigative_phase,
    )

    if investigative_phase_complete(game):
        return

    blocked_set_pre = {int(x) for x in blocked}
    investigative_sent_ids: List[int] = []
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        a_type = action.get("type")
        if actor_id in blocked:
            if a_type == "gaze" and game.player_roles.get(actor_id) == "Seer":
                if await _dm_actor_id(game, guild, actor_id, tos_msg.seer_gaze_interrupted()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
            elif a_type == "watch" and game.player_roles.get(actor_id) == "Lookout":
                if await _dm_actor_id(game, guild, actor_id, tos_msg.lookout_watch_interrupted()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
            elif a_type == "track" and game.player_roles.get(actor_id) == "Tracker":
                if await _dm_actor_id(game, guild, actor_id, tos_msg.tracker_track_interrupted()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
            elif a_type == "investigate":
                if await _dm_actor_id(game, guild, actor_id, tos_msg.investigate_interrupted()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
            continue
        if _investigative_sent_tonight(game, actor_id):
            continue

        if action.get("type") == "investigate":
            role = action.get("role")
            target_id = effective_primary_target(game, actor_id)
            if target_id is None:
                continue
            if role is None:
                continue
            target_role = game.player_roles.get(target_id, "Unknown")
            is_framed = game.role_states.get(target_id, {}).get("is_framed", False)
            is_doused = target_id in game.doused_players
            target_name = await _investigative_target_display_name(game, guild, target_id)

            if role == "Mole":
                if game.player_roles.get(actor_id) == "Mole" and not mole_investigate_eligible(
                    game, actor_id
                ):
                    continue
                revealed = target_role
                if is_doused or target_role == "Arsonist":
                    revealed = "Arsonist"
                from messages.role_catalog import consig_blurb

                feedback = consig_blurb(revealed)
                # Only decrement Mole uses if the actor is actually a Mole.
                # Chaos can inject a Mole-flavored investigate (see run_night_pipeline
                # chaos block), in which case the Chaos use has already been consumed
                # — decrementing here would double-charge Chaos (audit #19).
                if game.player_roles.get(actor_id) == "Mole":
                    state = game.role_states.get(actor_id, {})
                    if "uses_remaining" in state and not state.get("mole_used_this_night"):
                        state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                        state["mole_used_this_night"] = True
            elif role == "Sheriff":
                is_suspicious = is_framed or target_role in ALL_MAFIA_ROLES or is_doused or target_role == "Arsonist"
                feedback = (
                    tos_msg.sheriff_suspicious() if is_suspicious else tos_msg.sheriff_innocent()
                )
            else:
                # ToS-like Investigator: returns a bucket of possible roles.
                # In this bot, framing/dousing override the apparent bucket to mimic tampering.
                apparent_role = target_role
                if is_framed:
                    apparent_role = "Framer"
                elif is_doused or target_role == "Arsonist":
                    apparent_role = "Arsonist"

                bucket = investigator_bucket_for(apparent_role)
                feedback = tos_msg.investigator_result(target_name, bucket)
            if await _dm_actor_id(game, guild, actor_id, feedback):
                await _mirror_feedback_to_witch(game, guild, feedback, action.get("_controlled_by"))
                _mark_investigative_sent_tonight(game, actor_id)
                investigative_sent_ids.append(int(actor_id))

        elif action.get("type") == "watch":
            target_id = effective_primary_target(game, actor_id)
            if target_id is None:
                continue
            others = _lookout_visitors_excluding_self(visit_log.get(target_id, []), actor_id)
            if not others:
                msg = tos_msg.lookout_none()
            elif len(others) > LOOKOUT_VISITOR_CAP:
                msg = tos_msg.lookout_too_many()
            else:
                names = []
                for v in sorted(others, key=lambda pid: game.player_slots.get(int(pid), 9999)):
                    names.append(await _visitor_display_name(game, guild, int(v)))
                msg = tos_msg.lookout_visitors(", ".join(names))
            if await _dm_actor_id(game, guild, actor_id, msg):
                await _mirror_feedback_to_witch(game, guild, msg, action.get("_controlled_by"))
                _mark_investigative_sent_tonight(game, actor_id)
                investigative_sent_ids.append(int(actor_id))
        elif action.get("type") == "track":
            target_id = track_followed_player_id(action)
            if target_id is None:
                continue
            # Invert visit_log (target -> visitors) to get where a player went (visitor -> targets)
            visited_targets = [t for t, visitors in visit_log.items() if target_id in visitors]
            if not visited_targets:
                line = tos_msg.tracker_no_visit()
            else:
                names: List[str] = []
                for t_id in visited_targets:
                    names.append(await _visitor_display_name(game, guild, int(t_id)))
                if len(names) == 1:
                    line = tos_msg.tracker_visit(names[0])
                else:
                    formatted = ", ".join(f"**{n}**" for n in names)
                    line = tos_msg.tracker_visit_multiple(formatted)
            if await _dm_actor_id(game, guild, actor_id, line):
                await _mirror_feedback_to_witch(game, guild, line, action.get("_controlled_by"))
                _mark_investigative_sent_tonight(game, actor_id)
                investigative_sent_ids.append(int(actor_id))

        elif action.get("type") == "gaze":
            if game.player_roles.get(actor_id) != "Seer":
                continue
            dests = effective_visit_destinations_map(game).get(actor_id)
            if dests and len(dests) >= 2:
                a_id, b_id = dests[0], dests[1]
            else:
                raw = action.get("targets")
                if not isinstance(raw, list) or len(raw) < 2:
                    continue
                try:
                    a_id, b_id = int(raw[0]), int(raw[1])
                except (TypeError, ValueError):
                    continue
            mayor_block = False
            for tid in (a_id, b_id):
                stt = game.role_states.get(tid, {}) or {}
                if stt.get("is_revealed") and game.player_roles.get(tid) == "Mayor":
                    mayor_block = True
                    break
            if mayor_block:
                if await _dm_actor_id(game, guild, actor_id, tos_msg.seer_gaze_mayor_blocked()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
                continue
            raw_submitted = action.get("targets")
            if not isinstance(raw_submitted, list) or len(raw_submitted) < 2:
                continue
            try:
                submitted_a, submitted_b = int(raw_submitted[0]), int(raw_submitted[1])
            except (TypeError, ValueError):
                continue
            hist = game.role_states.setdefault(actor_id, {}).setdefault("seer_pair_history", [])
            key = tuple(sorted((submitted_a, submitted_b)))
            prior = {tuple(sorted((int(x[0]), int(x[1])))) for x in hist if isinstance(x, (list, tuple)) and len(x) == 2}
            if key in prior:
                if await _dm_actor_id(game, guild, actor_id, tos_msg.seer_gaze_duplicate_pair()):
                    _mark_investigative_sent_tonight(game, actor_id)
                    investigative_sent_ids.append(int(actor_id))
                continue

            ba = _seer_bucket_for_player(game, a_id)
            bb = _seer_bucket_for_player(game, b_id)
            if 4 in (ba, bb):
                msg = tos_msg.seer_gaze_enemies()
            elif ba == bb:
                msg = tos_msg.seer_gaze_friends()
            else:
                msg = tos_msg.seer_gaze_enemies()
            if await _dm_actor_id(game, guild, actor_id, msg):
                hist.append([submitted_a, submitted_b])
                _mark_investigative_sent_tonight(game, actor_id)
                investigative_sent_ids.append(int(actor_id))
                controller_id = action.get("_controlled_by")
                if controller_id is not None:
                    try:
                        wid = int(controller_id)
                    except (TypeError, ValueError):
                        wid = None
                    if wid is not None:
                        await _dm_actor_id(game, guild, wid, tos_msg.witch_stolen_gaze(msg))

    phase_done = investigative_phase_fulfilled(
        game, blocked_set_pre, investigative_sent_ids
    )
    await persist_post_investigative_phase(
        game,
        investigative_actor_ids=investigative_sent_ids,
        phase_complete=phase_done,
    )


async def resolve_killing(
    game: "Game",
    visit_log: Dict[int, List[int]],
    blocked: List[int],
    healed_by_map: Dict[int, int],
    protected_by_map: Dict[int, List[Dict[str, object]]],
    guild: discord.Guild,
) -> Set[int]:
    from engine.killing_resolve import resolve_killing as _resolve_killing_impl

    return await _resolve_killing_impl(
        game, visit_log, blocked, healed_by_map, protected_by_map, guild
    )


def _seer_apparent_role(game: "Game", pid: int) -> str:
    role = game.player_roles.get(pid, "Unknown")
    st = game.role_states.get(pid, {}) or {}
    if st.get("is_framed"):
        return "Framer"
    if role == "Arsonist":
        return "Arsonist"
    return role


def _seer_bucket(apparent: str) -> int:
    """1=friends town-ish, 2=mafia, 3=NK, 4=hostile neutrals (B4 short-circuit set)."""
    if apparent in SEER_HOSTILE_NEUTRAL_ROLES:
        return 4
    if apparent in SEER_NEUTRAL_KILLING_ROLES:
        return 3
    if apparent in ALL_MAFIA_ROLES:
        return 2
    friends = set(TOWN_ROLES) | set(SEER_FRIENDLY_EXTRA_ROLES)
    if apparent in friends:
        return 1
    return 4


def _seer_bucket_for_player(game: "Game", pid: int) -> int:
    """Bucket for gaze comparison; framed/doused overlay → Mafia bucket (B2)."""
    st = game.role_states.get(pid, {}) or {}
    if st.get("is_framed") or pid in game.doused_players:
        return 2
    return _seer_bucket(_seer_apparent_role(game, pid))


async def deliver_psychic_visions(game: "Game", guild: discord.Guild, blocked: Collection[int]) -> None:
    """Passive Psychic visions after deaths resolve; `blocked` is Escort-style roleblock list from the pipeline."""
    from night_resume import normalize_night_completion_snapshot
    from persist_schema import coerce_bool

    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    if snap is not None and coerce_bool(snap.get("psychic_visions_delivered")):
        game.psychic_visions_delivered_this_night = True
        return
    if getattr(game, "psychic_visions_delivered_this_night", False):
        return

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    living_set = set(int(x) for x in living_ids)

    psychic_ids = [pid for pid, r in game.player_roles.items() if r == "Psychic"]
    if not psychic_ids:
        return

    blocked_set = set(int(x) for x in blocked)

    def _slot(pid: int) -> str:
        try:
            return str(game.player_slots.get(int(pid), "?"))
        except (TypeError, ValueError):
            return "?"

    for psychic_id in psychic_ids:
        if psychic_id not in living_set:
            continue
        from messages import tos as tos_msg

        if psychic_id in blocked_set:
            await _dm_actor_id(game, guild, psychic_id, tos_msg.psychic_rb())
            continue

        if len(living_set) <= 3:
            msg = tos_msg.psychic_too_small_night()
            await _dm_actor_id(game, guild, psychic_id, msg)
            thief = game.role_states.get(psychic_id, {}).get("psychic_vision_recipient_id")
            try:
                tid = int(thief) if thief is not None else None
            except (TypeError, ValueError):
                tid = None
            if tid is not None and tid != psychic_id and tid in living_set:
                await _dm_actor_id(game, guild, tid, tos_msg.psychic_stolen_useless())
            continue

        odd_vision = int(getattr(game, "day_number", 0)) % 2 == 1
        pool_ex_psychic = living_set - {psychic_id}

        if odd_vision:
            evil_pool = [
                pid
                for pid in pool_ex_psychic
                if (
                    game.player_roles.get(pid) in ALL_MAFIA_ROLES
                    or game.player_roles.get(pid) in PSYCHIC_ODD_EVIL_NEUTRALS
                    or game.role_states.get(pid, {}).get("is_framed")
                    or pid in game.doused_players
                )
            ]
            if not evil_pool:
                msg = tos_msg.psychic_spirits_silent()
            elif len(pool_ex_psychic) < 3:
                msg = tos_msg.psychic_too_faint_three()
            else:
                e = random.choice(evil_pool)
                others = [x for x in pool_ex_psychic if x != e]
                if len(others) < 2:
                    msg = tos_msg.psychic_too_faint_three()
                else:
                    a, b = random.sample(others, 2)
                    slots = sorted({_slot(e), _slot(a), _slot(b)}, key=lambda s: int(s) if str(s).isdigit() else 10**9)
                    msg = tos_msg.psychic_vision_evil_slots(slots[0], slots[1], slots[2])
        else:
            from faction_taxonomy import psychic_even_night_good_role

            good_pool = [
                pid
                for pid in pool_ex_psychic
                if psychic_even_night_good_role(game.player_roles.get(pid) or "")
            ]
            if not good_pool:
                msg = tos_msg.psychic_too_evil()
            elif len(pool_ex_psychic) < 2:
                msg = tos_msg.psychic_too_faint_two()
            else:
                g = random.choice(good_pool)
                # Both named slots must be "good" pool (Town + Survivor); never label an evil slot as good.
                second_pool = [x for x in good_pool if x not in (psychic_id, g)]
                if not second_pool:
                    msg = tos_msg.psychic_too_faint_two()
                else:
                    h = random.choice(second_pool)
                    slots = sorted({_slot(g), _slot(h)}, key=lambda s: int(s) if str(s).isdigit() else 10**9)
                    msg = tos_msg.psychic_vision_good_slots(slots[0], slots[1])

        await _dm_actor_id(game, guild, psychic_id, msg)
        thief = game.role_states.get(psychic_id, {}).get("psychic_vision_recipient_id")
        try:
            wid = int(thief) if thief is not None else None
        except (TypeError, ValueError):
            wid = None
        if wid is not None and wid != psychic_id and wid in living_set:
            await _dm_actor_id(game, guild, wid, tos_msg.psychic_stolen_prefix(msg))


async def send_night_feedback(
    game: "Game",
    blocked: List[int],
    guild: discord.Guild,
    *,
    deaths: Optional[Set[int]] = None,
    healed_by_map: Optional[Dict[int, int]] = None,
) -> None:
    from messages import tos as tos_msg
    from night_resume import normalize_night_completion_snapshot
    from persist_schema import coerce_bool

    snap = normalize_night_completion_snapshot(
        getattr(game, "night_completion_snapshot", None)
    )
    if snap is not None and coerce_bool(snap.get("night_feedback_sent")):
        return

    blocked_set = set(int(x) for x in blocked)
    death_set = set(int(x) for x in (deaths or ()))
    heal_map = healed_by_map or {}
    living_ids_set: Set[int] = {
        int(m.id) for m in getattr(game, "living_players", []) or []
    }  # type: ignore[union-attr]

    for p_id in blocked_set:
        if living_ids_set and p_id not in living_ids_set:
            continue
        act = game.night_actions.get(p_id, {})
        if act.get("blocked_by_gatekeeper") and not act.get("blocked_by_roleblock"):
            continue
        a_type = act.get("type")
        role = game.player_roles.get(p_id)
        if _investigative_sent_tonight(game, int(p_id)) and (
            (a_type == "watch" and role == "Lookout")
            or (a_type == "track" and role == "Tracker")
            or (a_type == "gaze" and role == "Seer")
            or (a_type == "investigate" and role in ("Sheriff", "Investigator", "Mole"))
        ):
            continue
        player = await game.get_member_safe(guild, p_id)
        if player:
            try:
                await player.send(tos_msg.roleblocked())
            except discord.HTTPException:
                pass

            # Blocked visit (RB, GK, etc.): duel outcome alone does not award a win.
            if game.player_roles.get(p_id) == "Pirate" and act.get("duel_won"):
                try:
                    await player.send(tos_msg.pirate_plunder_blocked())
                except discord.HTTPException:
                    pass

    _TARGET_SURVIVAL_MSGS = {
        "survived": tos_msg.attacked_survived,
        "ga_ward": tos_msg.ga_ward_survived_attack,
        "healed": tos_msg.doctor_healed,
        "vest": tos_msg.vest_survived_attack,
        "alert": tos_msg.alert_survived_attack,
        "ignite_blocked": tos_msg.ga_ward_survived_attack,
        "witch_shield": tos_msg.witch_night1_shield,
        "chaos_shield": tos_msg.neutral_night1_shield,
        "jester_shield": tos_msg.neutral_night1_shield,
    }

    for p_id, state in list(game.role_states.items()):
        if int(p_id) in death_set:
            continue
        reason = state.get("attacked_tonight_reason")
        if not reason:
            continue
        player = await game.get_member_safe(guild, p_id)
        if not player:
            continue
        msg_fn = _TARGET_SURVIVAL_MSGS.get(str(reason))
        if not msg_fn:
            continue
        try:
            await player.send(msg_fn())
        except discord.HTTPException:
            pass
        if str(reason) == "healed":
            healer_id = heal_map.get(int(p_id))
            if healer_id is not None and int(healer_id) not in blocked_set:
                doctor = await game.get_member_safe(guild, int(healer_id))
                if doctor:
                    try:
                        await doctor.send(tos_msg.doctor_target_attacked())
                    except discord.HTTPException:
                        pass

    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "hypnotize" or actor_id in blocked:
            continue

        target_raw = action.get("target")
        try:
            target_id = int(target_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        target = await game.get_member_safe(guild, target_id)
        if not target:
            continue

        msg_type = action.get("msg_type")
        if not isinstance(msg_type, str):
            continue

        fake_msgs = {
            "healed": tos_msg.hypnotist_fake_healed(),
            "roleblocked": tos_msg.roleblocked(),
            "transported": tos_msg.transported(),
            "controlled": tos_msg.hypnotist_fake_controlled(),
            "attacked": tos_msg.hypnotist_fake_attacked(),
        }
        if msg_type not in fake_msgs:
            continue
        try:
            await target.send(fake_msgs[msg_type])
        except discord.HTTPException:
            pass


def clear_attacked_tonight_reasons(game: "Game") -> None:
    """Drop per-night survival reasons after DMs are sent or snapshotted for resume."""
    for st in game.role_states.values():
        if isinstance(st, dict):
            st.pop("attacked_tonight_reason", None)


def _clear_stale_night_combat_feedback(game: "Game") -> None:
    """Drop per-night attack feedback from a crashed prior resolve on the same night."""
    clear_attacked_tonight_reasons(game)


def snapshot_healed_by_map(healed_by: dict[int, int]) -> list[list[int]]:
    return [[int(t), int(h)] for t, h in healed_by.items()]


def restore_healed_by_map(raw: object) -> dict[int, int]:
    out: dict[int, int] = {}
    if not isinstance(raw, (list, tuple)):
        return out
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            out[int(pair[0])] = int(pair[1])
        except (TypeError, ValueError):
            continue
    return out


def snapshot_attacked_tonight_reasons(game: "Game") -> dict[str, str]:
    out: dict[str, str] = {}
    for pid, st in game.role_states.items():
        if not isinstance(st, dict):
            continue
        reason = st.get("attacked_tonight_reason")
        if reason:
            out[str(int(pid))] = str(reason)
    return out


def restore_attacked_tonight_reasons(game: "Game", reasons: object) -> None:
    if not isinstance(reasons, dict):
        return
    for pid_s, reason in reasons.items():
        try:
            pid = int(pid_s)
        except (TypeError, ValueError):
            continue
        if reason:
            game.role_states.setdefault(pid, {})["attacked_tonight_reason"] = str(reason)


async def run_night_pipeline(
    game: "Game",
    guild: discord.Guild,
    *,
    deliver_feedback: bool = True,
) -> Tuple[Dict[int, List[int]], List[int], Dict[int, int], Dict[int, List[Dict[str, object]]], Set[int]]:
    from night_engine_checkpoint import (
        blocked_from_snap,
        chaos_phase_complete,
        deaths_from_killing_checkpoint,
        gk_sk_witch_notify_complete,
        killing_phase_complete,
        misc_phase_complete,
        persist_gk_sk_witch_notify_complete,
        persist_post_chaos_phase,
        persist_post_killing_phase,
        persist_post_misc_phase,
        persist_transport_control_phase,
        restore_night_engine_phase_checkpoint,
        transport_control_phase_complete,
    )

    from night_engine_checkpoint import _merge_snap

    _merge_snap(game, {"night_engine_running": True, "pre_pipeline": True})

    restore_night_engine_phase_checkpoint(game)
    if not (misc_phase_complete(game) or killing_phase_complete(game)):
        _clear_stale_night_combat_feedback(game)
    # Audit #10 (revised after C1 regression review) — clear stale
    # chaos_used_this_night flags ONLY when uses_remaining is at the role's
    # STARTING value, which signals a crash-mid-resolve before the
    # decrement step ran.
    #
    # Background: chaos_used_this_night is the only per-night marker whose
    # presence hard-skips the entire chaos action (every other
    # *_used_this_night flag only gates counter decrement, so misc actions
    # remain idempotent across double-pipeline calls). For 4 of 10 chaos
    # effects (transport / protect / frame / hide) the original chaos
    # action stays in `night_actions` even after a legitimate first pass.
    # A naive "flag=True AND uses_remaining>0" clear would re-fire chaos
    # on the next pipeline call, double-decrementing uses and undoing a
    # transport swap. Anchoring on chaos_starting_uses(living_n) (1 at ≤7p, 2 above)
    # distinguishes the inconsistent crash-recovery state (uses=starting + flag)
    # from the legitimate post-first-pass state (uses=starting-1 + flag).
    # Do not clear night_transport_swaps here — start_night resets them, and
    # keeping swaps across an idempotent second resolve preserves Chaos transport.
    _invalidate_effective_visit_cache(game)
    _restore_chaos_transport_swaps(game)
    _clear_stale_per_night_action_flags(game)

    from night_resolve_prep import expand_reanimate_for_night_resolve, notify_reanimate_expand_failures

    failed_retri = expand_reanimate_for_night_resolve(game)
    await notify_reanimate_expand_failures(game, guild, failed_retri)

    from config import chaos_starting_uses
    from persist_schema import coerce_role_state_int

    living_n = len(getattr(game, "living_players", []) or []) or len(
        getattr(game, "player_roles", {}) or {}
    )
    chaos_starting = chaos_starting_uses(max(int(living_n), 1))
    for pid, st in list(game.role_states.items()):
        if not isinstance(st, dict):
            continue
        if game.player_roles.get(pid) != "Chaos":
            continue
        if (
            st.get("chaos_used_this_night")
            and coerce_role_state_int(st.get("uses_remaining"), 0) >= chaos_starting
        ):
            st["chaos_used_this_night"] = False

    blocked: List[int] = []
    if not transport_control_phase_complete(game):
        await resolve_transports(game, guild)
        await resolve_control(game, guild)
        visit_log_raw = build_visit_log(game)
        blocked = resolve_blocking(game, visit_log_raw)
        _prune_blocked_transport_swaps(game, blocked)
        await persist_transport_control_phase(game, blocked=blocked)
    else:
        blocked = blocked_from_snap(game)

    if not blocked:
        visit_log_raw = build_visit_log(game)
        blocked = resolve_blocking(game, visit_log_raw)

    # Chaos resolution (sim-aligned):
    # Chaos injects ONE disruptive effect involving their two targets.
    # - Chaos is not told which effect occurred
    # - Consume a use only if Chaos is not blocked and the action is valid
    #
    # We implement this by directly applying the effect to the game state and/or night_actions.
    # (Some effects like watch/track reuse existing action handling; role checks are intentionally not enforced.)
    blocked_set: Set[int] = set(blocked)
    chaos_ran_this_pass = False
    if not chaos_phase_complete(game):
        for actor_id, action in list(game.night_actions.items()):
            if action.get("type") != "chaos":
                continue
            if actor_id in blocked_set:
                if chaos_try_spend_use(game, actor_id, action):
                    chaos_ran_this_pass = True
                continue
            pair = chaos_targets_valid(action)
            if pair is None:
                continue
            t1, t2 = pair
            if not chaos_may_consume_use(game, actor_id, action):
                continue

            state = game.role_states.setdefault(actor_id, {})
            chaos_ran_this_pass = True
            rng = random.Random(f"{game.guild_id}:{game.day_number}:{actor_id}:{t1}:{t2}")
        # Chaos effect pool:
        # Keep it to effects with a clean 1-target or 2-target shape.
        # Exclude killing actions (kill/shoot/plunder/ignite) and self-only actions (vest/alert/clean),
        # and exclude "message composition" abilities like Hypnotist.
        # Exclude heal/protect: Chaos only chooses other players, so those read as random town-help
        # rather than disruption; guard covers "blocks visitors to t1" without gifting a heal.
            eff = rng.choice(CHAOS_EFFECT_POOL)

            if eff == "roleblock":
                game.night_actions[actor_id] = {
                    "type": "roleblock",
                    "actor": actor_id,
                    "target": t1,
                    "_from_chaos": True,
                }
            elif eff == "transport":
                state["chaos_transport_pair"] = [t1, t2]
                if _register_transport_swap(game, t1, t2, actor_id):
                    notified: Set[frozenset[int]] = getattr(game, "night_transport_dm_pairs", set())
                    pair_key = frozenset({int(t1), int(t2)})
                    if pair_key not in notified:
                        notified = set(notified)
                        notified.add(pair_key)
                        game.night_transport_dm_pairs = notified
                        for tid in (t1, t2):
                            m = await game.get_member_safe(guild, tid)
                            if m:
                                try:
                                    from messages import tos as tos_msg

                                    await m.send(tos_msg.transported())
                                except discord.HTTPException:
                                    pass
            elif eff == "watch":
                game.night_actions[actor_id] = {"type": "watch", "actor": actor_id, "target": t1}
            elif eff == "track":
                game.night_actions[actor_id] = {"type": "track", "actor": actor_id, "target": t1}
            elif eff == "investigate":
                game.night_actions[actor_id] = {
                    "type": "investigate",
                    "actor": actor_id,
                    "target": t1,
                    "role": "Investigator",
                }
            elif eff == "frame":
                subject = effective_visit_house_for_submitted_target(game, t1)
                game.role_states.setdefault(subject, {})["is_framed"] = True
            elif eff == "hide":
                subject = effective_visit_house_for_submitted_target(game, t1)
                game.role_states.setdefault(subject, {})["is_hidden_by_gravedigger"] = True
            elif eff == "guard":
                game.night_actions[actor_id] = {
                    "type": "guard",
                    "actor": actor_id,
                    "target": t1,
                    "_from_chaos": True,
                }

            # Consume a use only after the effect is applied (audit: persist-after-effect).
            if not chaos_try_spend_use(game, actor_id, action):
                continue
            state["chaos_visit_targets"] = [t1, t2]
            from night_engine_checkpoint import persist_chaos_visit_targets_progress

            await persist_chaos_visit_targets_progress(game)

            for tid in (t1, t2):
                m = await game.get_member_safe(guild, tid)
                if m:
                    from messages import tos as tos_msg

                    await _dm_player(m, tos_msg.chaos_touch())
        if chaos_ran_this_pass:
            await persist_post_chaos_phase(game)

    # Ensure any Chaos-injected direct blocks are reflected in the blocked_set.
    # (Chaos roleblock is injected as an action; the recompute below will incorporate it.)

    visit_log_raw, blocked = _rebuild_visits_blocking_after_chaos_mutations(game)
    living_set = {int(m.id) for m in getattr(game, "living_players", []) or []}
    gk_blocked, _rb_only = _compute_blocked_sets(game, visit_log_raw, living_set)
    if not gk_sk_witch_notify_complete(game):
        await notify_gatekeeper_blocked_visitors(game, guild, visit_log_raw, gk_blocked)
        await serial_killer_escort_counters(game, guild, blocked)
        await finalize_witch_control_feedback(game, guild, blocked)
        await persist_gk_sk_witch_notify_complete(game)

    # Effective visit log: roleblocked players do not "visit" for Lookout/Tracker/Alert semantics.
    visit_log = {
        t_id: [v_id for v_id in visitors if v_id not in blocked]
        for t_id, visitors in visit_log_raw.items()
    }

    healed_by_map, protected_by_map = await apply_misc_actions(game, blocked, guild)
    if not misc_phase_complete(game):
        await persist_post_misc_phase(
            game, healed_by_map=healed_by_map, protected_by_map=protected_by_map
        )

    await resolve_investigative(game, blocked, visit_log, guild)
    if killing_phase_complete(game):
        deaths = deaths_from_killing_checkpoint(game)
    else:
        deaths = await resolve_killing(
            game, visit_log, blocked, healed_by_map, protected_by_map, guild
        )
        await persist_post_killing_phase(
            game,
            deaths=deaths,
            blocked=blocked,
            healed_by_map=healed_by_map,
        )

    # Pirate personal win: duel won AND plunder kill landed (target died to pirate_plunder).
    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "plunder":
            continue
        if actor_id in blocked:
            continue
        if actor_id in deaths:
            continue
        if not action.get("duel_won", False):
            continue
        plunder_target = _coerce_int_id(action.get("target"))
        if plunder_target is None:
            continue
        if plunder_target not in deaths:
            continue
        if game.night_death_causes.get(plunder_target) != "pirate_plunder":
            continue
        state = game.role_states.setdefault(actor_id, {})
        if state.get("pirate_win_this_night"):
            continue
        state["wins"] = coerce_role_state_int(state.get("wins"), 0) + 1
        state["pirate_win_this_night"] = True

    from night_engine_checkpoint import persist_engine_complete_pending_feedback

    await persist_engine_complete_pending_feedback(
        game,
        deaths=deaths,
        blocked=blocked,
        healed_by_map=healed_by_map,
    )
    if deliver_feedback:
        await send_night_feedback(
            game, blocked, guild, deaths=deaths, healed_by_map=healed_by_map
        )
    return visit_log, blocked, healed_by_map, protected_by_map, deaths

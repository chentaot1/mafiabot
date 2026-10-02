from __future__ import annotations

import asyncio
import random
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import discord

from config import ALL_MAFIA_ROLES, CONTROL_IMMUNE_ROLES, ROLEBLOCK_IMMUNE_ROLES, TOWN_ROLES

if TYPE_CHECKING:
    from game import Game


async def resolve_transports(game: "Game", guild: discord.Guild) -> None:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    redirect_map: Dict[int, int] = {}
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if action.get("type") != "transport":
            continue

        targets = action.get("targets")
        if not isinstance(targets, list) or len(targets) != 2:
            # Corrupted/invalid persisted state safety: ignore malformed transport actions.
            continue
        try:
            pA_id, pB_id = int(targets[0]), int(targets[1])
        except (TypeError, ValueError):
            continue
        real_pA = redirect_map.get(pA_id, pA_id)
        real_pB = redirect_map.get(pB_id, pB_id)
        redirect_map[pA_id] = real_pB
        redirect_map[pB_id] = real_pA

        for p_id in [pA_id, pB_id]:
            m = await game.get_member_safe(guild, p_id)
            if m:
                try:
                    await m.send("You were transported to another location!")
                except discord.HTTPException:
                    pass

    if redirect_map:
        for act_actor_id, act in list(game.night_actions.items()):
            # ToS-like: some actions should not be redirected by Transporter.
            # Survivor vests always apply to self, even if transported.
            if game.player_roles.get(act_actor_id) == "Pirate" or act.get("type") in {"transport", "vest", "bg_vest", "clean"}:
                continue
            if "target" in act:
                try:
                    t = int(act["target"])
                except (TypeError, ValueError):
                    t = None
                if t is not None and t in redirect_map:
                    act["target"] = redirect_map[t]
            if "targets" in act:
                new_targets = []
                for t in act["targets"]:
                    try:
                        tid = int(t)
                    except (TypeError, ValueError):
                        new_targets.append(t)
                        continue
                    new_targets.append(redirect_map.get(tid, tid))
                act["targets"] = new_targets


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
        actor = await game.get_member_safe(guild, actor_id)
        controlled = await game.get_member_safe(guild, controlled_id)
        final_tgt = await game.get_member_safe(guild, final_target_id)

        if game.player_roles.get(controlled_id) in CONTROL_IMMUNE_ROLES:
            continue

        # If the Witch herself would be Gatekeeper-blocked (visiting a guarded
        # target), the control attempt fails. This matches resolve_blocking()'s
        # final outcome and correctly handles chained block scenarios.
        if actor_id in gk_blocked_pre:
            continue

        if controlled:
            try:
                await controlled.send("🧙 You felt a strange force take hold of you... You were **controlled** tonight.")
            except discord.HTTPException:
                pass

        # If the controlled player did not submit an action, Witch can still force certain roles
        # to act (ToS-like). Keep this narrow to avoid phantom-visit side effects.
        if controlled_id not in game.night_actions:
            forced = False
            controlled_role = game.player_roles.get(controlled_id)
            controlled_state = game.role_states.get(controlled_id, {})

            if controlled_role == "Vigilante":
                if final_target_id == controlled_id:
                    forced = False
                elif controlled_state.get("shots_remaining", 0) > 0 and not controlled_state.get("will_die_of_guilt"):
                    pending_actions.append(
                        (
                            controlled_id,
                            {"type": "shoot", "target": final_target_id, "actor": controlled_id, "forced_by_witch": True},
                        )
                    )
                    forced = True

            if forced:
                if actor and controlled and final_tgt:
                    try:
                        await actor.send(f"You successfully forced {controlled.display_name} to target {final_tgt.display_name}.")
                    except discord.HTTPException:
                        pass
                continue

        redirected = False
        for act_actor_id, act in list(game.night_actions.items()):
            if act_actor_id == controlled_id and act.get("type") != "control":
                # ToS-like: Survivor vest always targets self; Witch cannot retarget it.
                if act.get("type") in {"vest", "clean"}:
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
                    if actor and controlled and final_tgt:
                        try:
                            await actor.send(
                                f"You successfully prevented an ignite and forced {controlled.display_name} to douse {final_tgt.display_name}."
                            )
                        except discord.HTTPException:
                            pass
                    redirected = True
                    break
                if "target" in act:
                    act["target"] = final_target_id
                if "targets" in act:
                    # For multi-target actions, only force the primary target.
                    # (Transporter is control-immune in this ruleset, but keep this safe anyway.)
                    if act["targets"]:
                        act["targets"][0] = final_target_id
                # ToS-like: Witch receives the *results* the controlled target would have gotten.
                # Tag the controlled action so downstream investigative/watch resolution can mirror the DM.
                act["_controlled_by"] = actor_id
                if actor and controlled and final_tgt:
                    try:
                        await actor.send(f"You successfully forced {controlled.display_name} to target {final_tgt.display_name}.")
                    except discord.HTTPException:
                        pass
                redirected = True
                break

        if not redirected and actor and controlled:
            try:
                await actor.send(f"You attempted to control {controlled.display_name}, but they had no redirectable action.")
            except discord.HTTPException:
                pass

        real_role = game.player_roles.get(controlled_id, "Unknown")
        revealed_role = real_role
        if actor:
            try:
                await actor.send(f"You learned the role of your target: **{revealed_role}**.")
            except discord.HTTPException:
                pass

    # Apply pending actions safely after iterating
    for p_id, payload in pending_actions:
        if p_id in game.night_actions:
            game.night_actions[p_id].update(payload)
        else:
            game.night_actions[p_id] = payload


def build_visit_log(game: "Game") -> Dict[int, List[int]]:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    visit_log: Dict[int, List[int]] = {}
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if game.player_roles.get(actor_id) == "Gatekeeper":
            continue
        a_type = action.get("type")
        # Only count "visits" that represent a player going to another player.
        # Multi-target actions are handled explicitly to avoid polluting Lookout/Alert logic.
        # Self-only actions do not count as visits.
        # Guard actions are passive station-keeping (Gatekeeper / Chaos-as-guard), not visits.
        if a_type == "guard":
            continue
        if a_type in {"vest", "alert", "bg_vest", "clean"}:
            continue
        if a_type == "control":
            raw = action.get("targets")
            if not isinstance(raw, list) or not raw:
                targets = []
            else:
                targets = [raw[0]]
        elif a_type == "transport":
            raw = action.get("targets", [])
            targets = raw if isinstance(raw, list) else []
        else:
            targets = [action.get("target")]

        for t_id in targets:
            if t_id is None:
                continue
            # Corrupted/invalid persisted state safety: targets must be hashable ints.
            try:
                tid = int(t_id)
            except (TypeError, ValueError):
                continue
            visit_log.setdefault(tid, []).append(actor_id)
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
    blockers: List[Tuple[int, int]] = []
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if action.get("type") in {"roleblock", "plunder"}:
            target_id = action.get("target")
            if target_id is None:
                continue
            try:
                tid = int(target_id)
            except (TypeError, ValueError):
                continue
            blockers.append((actor_id, tid))

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
            target_raw = action.get("target")
            if target_raw is None:
                continue
            try:
                target_id = int(target_raw)
            except (TypeError, ValueError):
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
            for actor_id, target_id in blockers:
                if actor_id in blocked_set_local:
                    continue
                if game.player_roles.get(target_id) in ROLEBLOCK_IMMUNE_ROLES:
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
        if int(st.get("uses_remaining", 0)) <= 0:
            continue
        target_raw = action.get("target")
        if target_raw is None:
            continue
        try:
            int(target_raw)
        except (TypeError, ValueError):
            continue
        if "uses_remaining" in st:
            st["uses_remaining"] = max(0, int(st.get("uses_remaining", 0)) - 1)
            st["gatekeeper_used_this_night"] = True

    # Mark Gatekeeper blocks for feedback.
    for p_id in gatekeeper_blocked:
        game.night_actions.setdefault(p_id, {})["blocked_by_gatekeeper"] = True

    return list(blocked_set)


async def apply_misc_actions(
    game: "Game", blocked: List[int], guild: discord.Guild
) -> Tuple[Dict[int, int], Dict[int, List[Dict[str, object]]]]:
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

    # Deterministic priority: apply frames before other misc effects
    # so investigative outcomes don't depend on dict iteration order.
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if actor_id in blocked:
            continue
        if action.get("type") == "frame":
            tgt = _coerce_int(action.get("target"))
            if tgt is None:
                continue
            game.role_states.setdefault(tgt, {})["is_framed"] = True

    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if actor_id in blocked:
            continue
        a_type = action.get("type")

        if a_type == "heal":
            target_id = _coerce_int(action.get("target"))
            if target_id is None:
                continue
            # House rule: revealed Mayor cannot be healed (including Retributionist Doctor corpse / Chaos heals).
            if game.role_states.get(target_id, {}).get("is_revealed") and game.player_roles.get(target_id) == "Mayor":
                continue
            healed_by_map[target_id] = actor_id
            if target_id == actor_id:
                state = game.role_states.get(actor_id, {})
                if "self_heals_remaining" in state and not state.get("self_heal_used_this_night"):
                    state["self_heals_remaining"] = max(0, int(state.get("self_heals_remaining", 0)) - 1)
                    state["self_heal_used_this_night"] = True

        elif a_type == "protect":
            protected_target = _coerce_int(action.get("target"))
            if protected_target is None:
                continue
            protected_by_map.setdefault(protected_target, []).append({"id": actor_id, "dies_on_guard": True})
            state = game.role_states.get(actor_id, {})
            if protected_target == actor_id:
                if "self_protects_remaining" in state and not state.get("bg_self_protect_used_this_night"):
                    state["self_protects_remaining"] = max(0, int(state.get("self_protects_remaining", 0)) - 1)
                    state["bg_self_protect_used_this_night"] = True
            else:
                if "uses_remaining" in state and not state.get("bg_protect_used_this_night"):
                    state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                    state["bg_protect_used_this_night"] = True
        elif a_type == "ret_protect":
            protected_target = _coerce_int(action.get("target"))
            if protected_target is None:
                continue
            protected_by_map.setdefault(protected_target, []).append({"id": actor_id, "dies_on_guard": False})
        elif a_type == "bg_vest":
            # ToS-like Bodyguard self-protect: a one-time vest (no counterattack)
            state = game.role_states.get(actor_id, {})
            # Corrupted/persisted action safety: if the use count is 0, treat as inert.
            if int(state.get("self_protects_remaining", 0)) <= 0:
                continue
            game.role_states.setdefault(actor_id, {})["is_vested"] = True
            if "self_protects_remaining" in state and not state.get("bg_self_protect_used_this_night"):
                state["self_protects_remaining"] = max(0, int(state.get("self_protects_remaining", 0)) - 1)
                state["bg_self_protect_used_this_night"] = True

        elif a_type == "vest":
            state = game.role_states.get(actor_id, {})
            # Corrupted/persisted action safety: if the use count is 0, treat as inert.
            if int(state.get("vests_remaining", 0)) <= 0:
                continue
            game.role_states.setdefault(actor_id, {})["is_vested"] = True
            if "vests_remaining" in state and not state.get("vest_used_this_night"):
                state["vests_remaining"] = max(0, int(state.get("vests_remaining", 0)) - 1)
                state["vest_used_this_night"] = True

        elif a_type == "alert":
            state = game.role_states.get(actor_id, {})
            # Corrupted/persisted action safety: if the use count is 0, treat as inert.
            if int(state.get("alerts_remaining", 0)) <= 0:
                continue
            game.role_states.setdefault(actor_id, {})["is_on_alert"] = True
            if "alerts_remaining" in state and not state.get("alert_used_this_night"):
                state["alerts_remaining"] = max(0, int(state.get("alerts_remaining", 0)) - 1)
                state["alert_used_this_night"] = True

        elif a_type == "tailor":
            tgt = _coerce_int(action.get("target"))
            if tgt is None:
                continue
            fake_role = action.get("fake_role")
            if not isinstance(fake_role, str) or not fake_role:
                continue
            game.role_states.setdefault(tgt, {})["is_tailored_as"] = fake_role
            state = game.role_states.get(actor_id, {})
            if "uses_remaining" in state and not state.get("tailor_used_this_night"):
                state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                state["tailor_used_this_night"] = True

        elif a_type == "hide":
            tgt = _coerce_int(action.get("target"))
            if tgt is None:
                continue
            game.role_states.setdefault(tgt, {})["is_hidden_by_gravedigger"] = True
            state = game.role_states.get(actor_id, {})
            if "uses_remaining" in state and not state.get("gravedigger_used_this_night"):
                state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                state["gravedigger_used_this_night"] = True

        elif a_type == "douse":
            target_id = _coerce_int(action.get("target"))
            if target_id is None:
                continue
            if target_id not in game.doused_players:
                game.doused_players.add(target_id)
                target_member = await game.get_member_safe(guild, target_id)
                if target_member:
                    try:
                        await target_member.send("⛽ **You smell gasoline...**")
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


async def resolve_investigative(
    game: "Game", blocked: List[int], visit_log: Dict[int, List[int]], guild: discord.Guild
) -> None:
    living_ids_set: Set[int] = {int(m.id) for m in getattr(game, "living_players", []) or []}  # type: ignore[union-attr]
    for actor_id, action in list(game.night_actions.items()):
        if living_ids_set and actor_id not in living_ids_set:
            continue
        if actor_id in blocked:
            continue
        actor = await game.get_member_safe(guild, actor_id)
        if not actor:
            continue

        if action.get("type") == "investigate":
            role = action.get("role")
            try:
                target_id = int(action.get("target"))
            except (TypeError, ValueError):
                continue
            if role is None:
                continue
            target_role = game.player_roles.get(target_id, "Unknown")
            is_framed = game.role_states.get(target_id, {}).get("is_framed", False)
            is_doused = target_id in game.doused_players
            target_member = await game.get_member_safe(guild, target_id)
            target_name = target_member.display_name if target_member else "your target"

            if role == "Mole":
                revealed = target_role
                if is_doused or target_role == "Arsonist":
                    revealed = "Arsonist"
                feedback = f"Your investigation revealed that **{target_name}**'s role is **{revealed}**."
                state = game.role_states.get(actor_id, {})
                if "uses_remaining" in state and not state.get("mole_used_this_night"):
                    state["uses_remaining"] = max(0, int(state.get("uses_remaining", 0)) - 1)
                    state["mole_used_this_night"] = True
            elif role == "Sheriff":
                is_suspicious = is_framed or target_role in ALL_MAFIA_ROLES or is_doused or target_role == "Arsonist"
                feedback = f"Your target, **{target_name}**, seems {'**suspicious**' if is_suspicious else '**innocent**'}."
            else:
                # ToS-like Investigator: returns a bucket of possible roles.
                # In this bot, framing/dousing override the apparent bucket to mimic tampering.
                display_name = target_name

                def bucket_for(r: str) -> List[str]:
                    # Buckets are tuned to this bot's role list (no Vampires/Coven/etc).
                    # Keep them small (3-5) to remain useful in 5–8 player lobbies.
                    buckets: List[List[str]] = [
                        ["Investigator", "Mole", "Mayor", "Tracker"],
                        ["Doctor", "Bodyguard", "Survivor"],
                        ["Escort", "Consort", "Hypnotist"],
                        ["Lookout", "Transporter", "Tailor"],
                        ["Vigilante", "Pirate", "Scary Grandma"],
                        ["Mobster", "Gatekeeper", "Gravedigger"],
                        ["Framer", "Jester", "Executioner", "Witch", "Chaos"],
                        # ToS-like: Arsonist shares results with a strong defense/killing bucket.
                        # This bot has no Godfather, so we use Mobster as the closest analogue.
                        ["Bodyguard", "Mobster", "Arsonist"],
                        ["Retributionist", "Sheriff"],
                    ]
                    for b in buckets:
                        if r in b:
                            return b
                    return [r]

                apparent_role = target_role
                # ToS-like priority: frames override douses.
                if is_framed:
                    apparent_role = "Framer"
                elif is_doused or target_role == "Arsonist":
                    apparent_role = "Arsonist"

                bucket = bucket_for(apparent_role)
                feedback = f"Your investigation found clues that **{display_name}** could be: {', '.join(f'**{x}**' for x in bucket)}."
            try:
                await actor.send(feedback)
            except discord.HTTPException:
                pass
            # ToS-like Witch rule: if this investigative action was redirected via control,
            # send the Witch the same result the investigator received.
            controller_id = action.get("_controlled_by")
            if controller_id is not None:
                try:
                    wid = int(controller_id)
                except (TypeError, ValueError):
                    wid = None
                if wid is not None:
                    witch = await game.get_member_safe(guild, wid)
                    if witch:
                        try:
                            await witch.send(feedback)
                        except discord.HTTPException:
                            pass

        elif action.get("type") == "watch":
            try:
                target_id = int(action.get("target"))
            except (TypeError, ValueError):
                continue
            visitors = visit_log.get(target_id, [])
            if not visitors:
                msg = "Nobody visited your target tonight."
                try:
                    await actor.send(msg)
                except discord.HTTPException:
                    pass
                controller_id = action.get("_controlled_by")
                if controller_id is not None:
                    try:
                        wid = int(controller_id)
                    except (TypeError, ValueError):
                        wid = None
                    if wid is not None:
                        witch = await game.get_member_safe(guild, wid)
                        if witch:
                            try:
                                await witch.send(msg)
                            except discord.HTTPException:
                                pass
            else:
                names = []
                for v in visitors:
                    m = await game.get_member_safe(guild, v)
                    if m:
                        names.append(m.display_name)
                msg = f"The following people visited your target: {', '.join(names)}"
                try:
                    await actor.send(msg)
                except discord.HTTPException:
                    pass
                controller_id = action.get("_controlled_by")
                if controller_id is not None:
                    try:
                        wid = int(controller_id)
                    except (TypeError, ValueError):
                        wid = None
                    if wid is not None:
                        witch = await game.get_member_safe(guild, wid)
                        if witch:
                            try:
                                await witch.send(msg)
                            except discord.HTTPException:
                                pass
        elif action.get("type") == "track":
            try:
                target_id = int(action.get("target"))
            except (TypeError, ValueError):
                continue
            # Invert visit_log (target -> visitors) to get where a player went (visitor -> targets)
            visited_targets = [t for t, visitors in visit_log.items() if target_id in visitors]
            if not visited_targets:
                try:
                    await actor.send("Your target did not visit anyone tonight.")
                except discord.HTTPException:
                    pass
            else:
                names: List[str] = []
                for t_id in visited_targets:
                    m = await game.get_member_safe(guild, t_id)
                    if m:
                        names.append(m.display_name)
                try:
                    await actor.send(f"Your target visited: {', '.join(names)}")
                except discord.HTTPException:
                    pass


async def resolve_killing(
    game: "Game",
    visit_log: Dict[int, List[int]],
    blocked: List[int],
    healed_by_map: Dict[int, int],
    protected_by_map: Dict[int, List[Dict[str, object]]],
    guild: discord.Guild,
) -> Set[int]:
    deaths: Set[int] = set()
    kill_targets: Set[int] = set()
    attackers_on_bg: Dict[int, List[int]] = {}  # protected_target -> [attacker_ids...]
    attempted_kills: List[Tuple[int, int, str]] = []  # (actor_id, target_id, type)
    successful_heals: List[Tuple[int, int]] = []  # (healer_id, healed_target_id)
    healed_but_died_unstoppable: List[Tuple[int, int]] = []  # (healer_id, target_id)
    ignite_deaths: Set[int] = set()
    ignite_killers: Set[int] = set()

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    living_ids_set: Set[int] = set(int(x) for x in living_ids)

    for actor_id, action in list(game.night_actions.items()):
        # Major safety: ignore persisted actions from dead/non-living players.
        if actor_id not in living_ids_set:
            continue
        if action.get("type") == "ignite" and actor_id not in blocked:
            ignite_killers.add(actor_id)
            ignite_deaths = set(p_id for p_id in living_ids if p_id in game.doused_players)
            # ToS-like quirk (sim-aligned): if the Arsonist is doused, igniting burns them too.
            if actor_id in game.doused_players:
                ignite_deaths.add(actor_id)
            deaths.update(ignite_deaths)
            game.doused_players.clear()

    for p_id, state in game.role_states.items():
        if state.get("is_on_alert"):
            kill_targets.update(v for v in visit_log.get(p_id, []))

    for actor_id, action in list(game.night_actions.items()):
        if actor_id in blocked:
            continue
        # Major safety: ignore persisted actions from dead/non-living players.
        if actor_id not in living_ids_set:
            continue
        a_type = action.get("type")

        if a_type in ["shoot", "kill", "plunder"]:
            if a_type == "plunder" and not action.get("duel_won", False):
                continue
            if a_type == "shoot":
                # Corrupted/persisted action safety: a "shoot" action should not execute with 0 bullets.
                state = game.role_states.get(actor_id, {})
                if "shots_remaining" in state and int(state.get("shots_remaining", 0)) <= 0:
                    continue

            try:
                target_id = int(action.get("target"))
            except (TypeError, ValueError):
                continue
            # Major safety: never attack non-living / non-player ids.
            if target_id not in living_ids_set:
                continue
            attempted_kills.append((actor_id, target_id, a_type))
            if target_id in protected_by_map:
                attackers_on_bg.setdefault(target_id, []).append(actor_id)
            else:
                kill_targets.add(target_id)

            if a_type == "shoot":
                state = game.role_states.get(actor_id, {})
                if "shots_remaining" in state and not state.get("vig_shot_used_this_night"):
                    state["shots_remaining"] = max(0, int(state.get("shots_remaining", 0)) - 1)
                    state["vig_shot_used_this_night"] = True

    for protected_target, attackers in attackers_on_bg.items():
        if not attackers:
            continue
        bg_entries = [e for e in protected_by_map.get(protected_target, [])]
        bg_ids = []
        for e in bg_entries:
            try:
                bg_id = int(e.get("id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if bg_id in blocked or bg_id == protected_target:
                continue
            bg_ids.append(bg_id)

        if bg_ids:
            # Deterministic: the first Bodyguard in the list counters. Others get feedback only.
            bg_actor_id = bg_ids[0]
            # Normal Bodyguard dies on guard; Retributionist-using-BG-corpse does not.
            dies_on_guard = True
            for e in bg_entries:
                try:
                    if int(e.get("id")) == bg_actor_id:
                        dies_on_guard = bool(e.get("dies_on_guard", True))
                        break
                except (TypeError, ValueError):
                    continue
            if dies_on_guard:
                kill_targets.add(bg_actor_id)

            bg_member = await game.get_member_safe(guild, bg_actor_id)
            protected_member = await game.get_member_safe(guild, protected_target)
            if bg_member:
                try:
                    await bg_member.send("You fought off an attacker while guarding your target!")
                except discord.HTTPException:
                    pass
            if protected_member:
                try:
                    await protected_member.send("Someone protected you!")
                except discord.HTTPException:
                    pass

            for extra_bg_id in bg_ids[1:]:
                extra_bg = await game.get_member_safe(guild, extra_bg_id)
                if extra_bg:
                    try:
                        await extra_bg.send("Someone else protected your target first. You did not engage an attacker tonight.")
                    except discord.HTTPException:
                        pass

        for attacker_id in attackers:
            attacker = await game.get_member_safe(guild, attacker_id)
            if attacker and game.player_roles.get(attacker_id) == "Pirate":
                act = game.night_actions.get(attacker_id, {})
                if act.get("type") == "plunder" and act.get("duel_won", False):
                    try:
                        await attacker.send("You won your duel, but a Bodyguard killed you before you could finish the plunder.")
                    except discord.HTTPException:
                        pass
                else:
                    try:
                        await attacker.send("You were killed by a Bodyguard.")
                    except discord.HTTPException:
                        pass
            elif attacker:
                try:
                    await attacker.send("You were killed by a Bodyguard.")
                except discord.HTTPException:
                    pass
            deaths.add(attacker_id)

    for target_id in kill_targets:
        if target_id not in healed_by_map and not game.role_states.get(target_id, {}).get("is_vested") and not game.role_states.get(target_id, {}).get("is_on_alert"):
            # ToS-like: Arsonist has basic defense (immune to normal kills).
            # This bot currently has no "powerful attack" sources, so treat all night kills as normal.
            if game.player_roles.get(target_id) == "Arsonist":
                game.role_states.setdefault(target_id, {})["attacked_tonight_reason"] = "survived"
                continue
            # Witch Night 1 defense: block the first normal kill on Night 1.
            if game.day_number == 1 and game.player_roles.get(target_id) == "Witch":
                state = game.role_states.setdefault(target_id, {})
                if not state.get("night1_shield_used", False):
                    state["night1_shield_used"] = True
                    state["attacked_tonight_reason"] = "witch_shield"
                    witch_member = await game.get_member_safe(guild, target_id)
                    if witch_member:
                        try:
                            await witch_member.send("🛡️ Your mystical barrier protected you from an attack!")
                        except discord.HTTPException:
                            pass
                    continue
            deaths.add(target_id)

        else:
            # Target was attacked but survived (healed/vested/on-alert, etc.)
            # If healed prevented the kill, we'll send ToS-like Doctor feedback later.
            if target_id in healed_by_map:
                successful_heals.append((healed_by_map[target_id], target_id))
                game.role_states.setdefault(target_id, {})["attacked_tonight_reason"] = "healed"
            else:
                game.role_states.setdefault(target_id, {})["attacked_tonight_reason"] = "survived"

    # Unstoppable ignite: if a Doctor healed an ignite victim, give explicit feedback (heal had no effect).
    for tgt in ignite_deaths:
        healer_id = healed_by_map.get(tgt)
        if healer_id is not None:
            healed_but_died_unstoppable.append((healer_id, tgt))
            game.role_states.setdefault(tgt, {})["attacked_tonight_reason"] = "ignite"

    # --- Attacker failure feedback (ToS-like) ---
    for actor_id, target_id, a_type in attempted_kills:
        if actor_id in blocked:
            continue
        # Vigilante guilt (ToS-like): only if the shot actually killed a Town member.
        if a_type == "shoot" and target_id in deaths and game.player_roles.get(target_id) in TOWN_ROLES:
            game.role_states.setdefault(actor_id, {})["guilty_tomorrow"] = True
        # If target died, the attack succeeded.
        if target_id in deaths:
            continue
        # If the attacker died (e.g. by alert/BG), no need to DM them.
        if actor_id in deaths:
            continue

        defended = (
            target_id in healed_by_map
            or game.role_states.get(target_id, {}).get("is_vested")
            or game.role_states.get(target_id, {}).get("is_on_alert")
            or game.player_roles.get(target_id) == "Arsonist"
            or (game.player_roles.get(target_id) == "Witch" and game.day_number == 1 and game.role_states.get(target_id, {}).get("night1_shield_used"))
        )
        if defended:
            attacker = await game.get_member_safe(guild, actor_id)
            if attacker:
                try:
                    await attacker.send("Your target's defense was too strong to kill.")
                except discord.HTTPException:
                    pass

    # --- Doctor-specific feedback (ToS-like) ---
    for healer_id, healed_tgt in successful_heals:
        if healer_id in blocked:
            continue
        doctor = await game.get_member_safe(guild, healer_id)
        if doctor:
            try:
                await doctor.send("Your target was attacked last night!")
            except discord.HTTPException:
                pass

        healed_member = await game.get_member_safe(guild, healed_tgt)
        if healed_member:
            try:
                await healed_member.send("You were attacked but someone nursed you back to health!")
            except discord.HTTPException:
                pass

    for healer_id, dead_tgt in healed_but_died_unstoppable:
        if healer_id in blocked:
            continue
        doctor = await game.get_member_safe(guild, healer_id)
        if doctor:
            try:
                await doctor.send("Your target was killed by an unstoppable force — your heal had no effect.")
            except discord.HTTPException:
                pass

    # Arsonist feedback: if a doused-on-alert Scary Grandma dies to ignite, mention it (strategy clarity).
    if ignite_killers and ignite_deaths:
        for killer_id in ignite_killers:
            arso = await game.get_member_safe(guild, killer_id)
            if not arso:
                continue
            for dead_id in ignite_deaths:
                if game.player_roles.get(dead_id) == "Scary Grandma" and game.role_states.get(dead_id, {}).get("is_on_alert"):
                    try:
                        await arso.send("One of your victims was on alert, but your ignition burned through their defense.")
                    except discord.HTTPException:
                        pass

    return deaths


async def send_night_feedback(game: "Game", blocked: List[int], guild: discord.Guild) -> None:
    for p_id in set(blocked):
        player = await game.get_member_safe(guild, p_id)
        if player:
            try:
                await player.send("Someone occupied your night. You were **roleblocked!**")
            except discord.HTTPException:
                pass

            # Pirate Phantom Win Notification
            if game.player_roles.get(p_id) == "Pirate" and game.night_actions.get(p_id, {}).get("duel_won"):
                try:
                    await player.send(
                        "You won your duel, but something blocked your visit. The plunder was unsuccessful."
                    )
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
            "healed": "You were attacked but someone healed you!",
            "roleblocked": "Someone occupied your night. You were **roleblocked!**",
            "transported": "You were transported to another location!",
            "controlled": "🧙 You felt a strange force take hold of you... You were **controlled** tonight.",
            "attacked": "You were attacked but survived!",
        }
        try:
            await target.send(fake_msgs.get(msg_type, ""))
        except discord.HTTPException:
            pass

    # Real feedback: attacked-but-survived notifications
    for p_id, state in list(game.role_states.items()):
        reason = state.get("attacked_tonight_reason")
        if not reason:
            continue
        player = await game.get_member_safe(guild, p_id)
        if not player:
            continue
        try:
            # Keep messaging consistent: the Doctor-specific "nursed back to health" is sent elsewhere.
            if reason == "survived":
                await player.send("You were attacked but survived!")
        except discord.HTTPException:
            pass


async def run_night_pipeline(
    game: "Game", guild: discord.Guild
) -> Tuple[Dict[int, List[int]], List[int], Dict[int, int], Dict[int, List[Dict[str, object]]], Set[int]]:
    await resolve_transports(game, guild)
    await resolve_control(game, guild)
    visit_log_raw = build_visit_log(game)
    blocked = resolve_blocking(game, visit_log_raw)

    # Chaos resolution (sim-aligned):
    # Chaos injects ONE disruptive effect involving their two targets.
    # - Chaos is not told which effect occurred
    # - Consume a use only if Chaos is not blocked and the action is valid
    #
    # We implement this by directly applying the effect to the game state and/or night_actions.
    # (Some effects like watch/track/heal reuse existing action handling; role checks are intentionally not enforced.)
    blocked_set: Set[int] = set(blocked)
    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "chaos":
            continue
        if actor_id in blocked_set:
            continue
        targets = action.get("targets")
        if not isinstance(targets, list) or len(targets) != 2:
            continue
        try:
            t1 = int(targets[0])
            t2 = int(targets[1])
        except (TypeError, ValueError):
            continue
        if t1 == t2:
            continue

        state = game.role_states.setdefault(actor_id, {})
        if state.get("chaos_used_this_night"):
            continue
        if int(state.get("uses_remaining", 0)) <= 0:
            continue

        rng = random.Random(f"{game.guild_id}:{game.day_number}:{actor_id}:{t1}:{t2}")
        # Chaos effect pool:
        # Keep it to effects with a clean 1-target or 2-target shape.
        # Exclude killing actions (kill/shoot/plunder/ignite) and self-only actions (vest/alert/clean),
        # and exclude "message composition" abilities like Hypnotist.
        eff_pool = [
            "roleblock",
            "transport",
            "heal",
            "protect",
            "investigate",
            "watch",
            "track",
            "frame",
            "hide",
            "guard",
        ]
        eff = rng.choice(eff_pool)

        # Consume a use once the action is valid and Chaos isn't blocked,
        # even if the chosen effect ends up doing nothing (immune targets, blocked heals, etc.).
        state["uses_remaining"] = int(state.get("uses_remaining", 0)) - 1
        state["chaos_used_this_night"] = True

        # Tell both targets that Chaos touched them tonight (without revealing the effect).
        # Targets can't tell which side Chaos is "helping" because Chaos wins with everyone,
        # so this creates a social/bargaining dynamic rather than directional info.
        for tid in (t1, t2):
            m = await game.get_member_safe(guild, tid)
            if m:
                try:
                    await m.send("🌀 You felt the touch of Chaos tonight. Something — you can't tell what — was set into motion.")
                except discord.HTTPException:
                    pass

        if eff == "roleblock":
            # Inject a real roleblock action and let resolve_blocking handle immunities/chains.
            game.night_actions[actor_id] = {"type": "roleblock", "actor": actor_id, "target": t1, "_from_chaos": True}
        elif eff == "transport":
            # Apply a transport-like redirection by performing the same swap transform as Transporter.
            # This happens after normal transport resolution, but still affects downstream action handling.
            redirect_map = {t1: t2, t2: t1}
            for act_actor_id, act in list(game.night_actions.items()):
                if act_actor_id == actor_id:
                    continue
                if game.player_roles.get(act_actor_id) == "Pirate" or act.get("type") in {"transport", "vest", "bg_vest", "clean"}:
                    continue
                if "target" in act:
                    try:
                        tgt = int(act.get("target"))
                    except (TypeError, ValueError):
                        tgt = None
                    if tgt is not None and tgt in redirect_map:
                        act["target"] = redirect_map[tgt]
                if "targets" in act:
                    raw = act.get("targets")
                    if isinstance(raw, list):
                        new_targets = []
                        for x in raw:
                            try:
                                xi = int(x)
                            except (TypeError, ValueError):
                                new_targets.append(x)
                                continue
                            new_targets.append(redirect_map.get(xi, xi))
                        act["targets"] = new_targets
            # Transport messages (misinformation-ish but consistent with real transport feedback)
            for tid in (t1, t2):
                m = await game.get_member_safe(guild, tid)
                if m:
                    try:
                        await m.send("You were transported to another location!")
                    except discord.HTTPException:
                        pass
        elif eff == "heal":
            game.night_actions[actor_id] = {"type": "heal", "actor": actor_id, "target": t1}
        elif eff == "watch":
            game.night_actions[actor_id] = {"type": "watch", "actor": actor_id, "target": t1}
        elif eff == "track":
            game.night_actions[actor_id] = {"type": "track", "actor": actor_id, "target": t1}
        elif eff == "investigate":
            game.night_actions[actor_id] = {
                "type": "investigate",
                "actor": actor_id,
                "target": t1,
                "role": rng.choice(["Sheriff", "Investigator", "Mole"]),
            }
        elif eff == "protect":
            # Apply protection without consuming Bodyguard uses (Chaos is not a Bodyguard).
            # We'll attach it directly to protected_by_map later via role_states marker.
            game.role_states.setdefault(t1, {})["chaos_protected_by"] = int(actor_id)
        elif eff == "frame":
            game.role_states.setdefault(t1, {})["is_framed"] = True
        elif eff == "hide":
            game.role_states.setdefault(t1, {})["is_hidden_by_gravedigger"] = True
        elif eff == "guard":
            # Chaos-injected guard on t1, equivalent to a Gatekeeper guard for blocking
            # purposes. `_from_chaos` lets _compute_blocked_sets()/_gatekeeper_blocks()
            # honor it without requiring the actor's role to be "Gatekeeper", and prevents
            # Gatekeeper-use accounting from consuming uses on this action.
            game.night_actions[actor_id] = {
                "type": "guard",
                "actor": actor_id,
                "target": t1,
                "_from_chaos": True,
            }

    # Ensure any Chaos-injected direct blocks are reflected in the blocked_set.
    # (Chaos roleblock is injected as an action; the recompute below will incorporate it.)

    # Recompute visits + blocking after Chaos may have redirected targets or injected roleblocks.
    visit_log_raw = build_visit_log(game)
    blocked = resolve_blocking(game, visit_log_raw)

    # Effective visit log: roleblocked players do not "visit" for Lookout/Tracker/Alert semantics.
    visit_log = {
        t_id: [v_id for v_id in visitors if v_id not in blocked]
        for t_id, visitors in visit_log_raw.items()
    }

    # Pirate personal win accounting:
    # Count a "plunder win" when the Pirate wins the duel, even if the target later survives due to defense/heal/etc.
    # Note: Pirate is roleblock-immune to normal roleblocks, but can still be blocked by Gatekeeper guarding the target.
    for actor_id, action in list(game.night_actions.items()):
        if action.get("type") != "plunder":
            continue
        if actor_id in blocked:
            continue
        if not action.get("duel_won", False):
            continue
        # Guard against double-counting if the pipeline is invoked twice in the same night.
        state = game.role_states.setdefault(actor_id, {})
        if state.get("pirate_win_this_night"):
            continue
        state["wins"] = int(state.get("wins", 0)) + 1
        state["pirate_win_this_night"] = True

    healed_by_map, protected_by_map = await apply_misc_actions(game, blocked, guild)

    # Chaos "protect" effect: apply after misc action collection so it contributes to kill resolution.
    for pid, st in list(game.role_states.items()):
        by = st.get("chaos_protected_by")
        if by is None:
            continue
        try:
            by_id = int(by)
        except (TypeError, ValueError):
            continue
        protected_by_map.setdefault(int(pid), []).append({"id": by_id, "dies_on_guard": True})

    await resolve_investigative(game, blocked, visit_log, guild)
    deaths = await resolve_killing(game, visit_log, blocked, healed_by_map, protected_by_map, guild)
    await send_night_feedback(game, blocked, guild)
    return visit_log, blocked, healed_by_map, protected_by_map, deaths

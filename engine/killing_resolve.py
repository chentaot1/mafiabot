"""Night killing resolution (extracted from ``engine.night`` for maintainability)."""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import discord

from engine import combat as combat_tiers
from engine.night import _coerce_int_id

if TYPE_CHECKING:
    from game import Game


def _night_helpers():
    from engine import night as night_mod

    return night_mod.effective_primary_target, night_mod._dm_player

# Death-cause priority (documented): direct/ignite use assignment; BG counter uses
# assignment; alert visitor kills use setdefault (never overwrite an existing tag).


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
    attackers_on_bg: Dict[int, List[int]] = {}
    attempted_kills: List[Tuple[int, int, str]] = []
    healed_but_died_to_ignite: List[Tuple[int, int]] = []
    ignite_deaths: Set[int] = set()
    ignite_killers: Set[int] = set()
    alert_visitor_targets: Set[int] = set()

    effective_primary_target, _dm_player = _night_helpers()

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    living_ids_set: Set[int] = set(int(x) for x in living_ids)
    game.night_death_causes.clear()

    for actor_id, action in list(game.night_actions.items()):
        if actor_id not in living_ids_set:
            continue
        if action.get("type") == "ignite" and actor_id not in blocked:
            ignite_killers.add(actor_id)
            ignite_tier = combat_tiers.ignite_attack_tier()
            ignite_candidates = set(p_id for p_id in living_ids if p_id in game.doused_players)
            if actor_id in game.doused_players:
                ignite_candidates.add(actor_id)
            for p_id in ignite_candidates:
                if p_id in deaths:
                    continue
                if not combat_tiers.night_attack_would_kill(
                    game,
                    p_id,
                    ignite_tier,
                    healed=p_id in healed_by_map,
                ):
                    combat_tiers.set_survived_attack_reason(
                        game,
                        p_id,
                        ignite_tier,
                        healed=p_id in healed_by_map,
                    )
                    continue
                ignite_deaths.add(p_id)
                deaths.add(p_id)
                game.night_death_causes[p_id] = "arsonist_ignite"
                game.doused_players.discard(p_id)

    # Host may die to ignite earlier this phase; alert still shoots visitors (ToS-like).
    for p_id, state in game.role_states.items():
        if state.get("is_on_alert"):
            for v in visit_log.get(p_id, []):
                alert_visitor_targets.add(v)

    for actor_id, action in list(game.night_actions.items()):
        if actor_id not in living_ids_set:
            continue
        if actor_id in blocked:
            continue
        if action.get("type") != "plunder":
            continue
        try:
            tid = int(action.get("target"))
        except (TypeError, ValueError):
            continue
        if game.player_roles.get(tid) != "Serial Killer":
            continue
        if tid in deaths:
            continue
        sk_st = game.role_states.setdefault(tid, {})
        if action.get("duel_won", False):
            sk_st["sk_counter_kills"] = []
            sk_st["sk_suppressed_by_pirate"] = True
        else:
            if not sk_st.get("sk_cautious"):
                li = sk_st.setdefault("sk_counter_kills", [])
                if actor_id not in li:
                    li.append(actor_id)

    for actor_id, action in list(game.night_actions.items()):
        if actor_id in blocked:
            continue
        if actor_id not in living_ids_set:
            continue
        a_type = action.get("type")

        if a_type in ["shoot", "kill", "plunder", "sk_kill"]:
            if a_type == "sk_kill" and game.role_states.get(actor_id, {}).get("sk_suppressed_by_pirate"):
                continue
            if a_type == "plunder" and not action.get("duel_won", False):
                continue
            if a_type == "shoot":
                state = game.role_states.get(actor_id, {})
                if "shots_remaining" in state and int(state.get("shots_remaining", 0)) <= 0:
                    continue

            if a_type == "plunder":
                target_id = _coerce_int_id(action.get("target"))
            else:
                target_id = effective_primary_target(game, actor_id)
            if target_id is None:
                continue
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

    for sk_id, sk_role in list(game.player_roles.items()):
        if sk_role != "Serial Killer":
            continue
        if sk_id in deaths:
            continue
        for raw_vid in list(game.role_states.get(sk_id, {}).get("sk_counter_kills") or []):
            try:
                escort_id = int(raw_vid)
            except (TypeError, ValueError):
                continue
            if escort_id not in living_ids_set or escort_id in deaths:
                continue
            attempted_kills.append((sk_id, escort_id, "sk_counter"))
            if escort_id in protected_by_map:
                attackers_on_bg.setdefault(escort_id, []).append(sk_id)
            else:
                kill_targets.add(escort_id)

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
            bg_actor_id = bg_ids[0]
            dies_on_guard = True
            retri_notify_id: Optional[int] = None
            for e in bg_entries:
                try:
                    if int(e.get("id")) == bg_actor_id:
                        dies_on_guard = bool(e.get("dies_on_guard", True))
                        raw_retri = e.get("retri_actor_id")
                        if raw_retri is not None:
                            retri_notify_id = int(raw_retri)
                        break
                except (TypeError, ValueError):
                    continue
            if dies_on_guard and bg_actor_id in living_ids_set:
                deaths.add(bg_actor_id)
                game.night_death_causes.setdefault(bg_actor_id, "bodyguard_guard")

            notify_guard_id = retri_notify_id if retri_notify_id is not None else bg_actor_id
            bg_member = await game.get_member_safe(guild, notify_guard_id)
            protected_member = await game.get_member_safe(guild, protected_target)
            if bg_member:
                from messages import tos as tos_msg

                try:
                    await bg_member.send(tos_msg.bodyguard_fought_off())
                except discord.HTTPException:
                    pass
            if protected_member:
                from messages import tos as tos_msg

                try:
                    await protected_member.send(tos_msg.bodyguard_someone_protected_you())
                except discord.HTTPException:
                    pass

            for extra_bg_id in bg_ids[1:]:
                extra_bg = await game.get_member_safe(guild, extra_bg_id)
                if extra_bg:
                    from messages import tos as tos_msg

                    try:
                        await extra_bg.send(tos_msg.bodyguard_other_bg_first())
                    except discord.HTTPException:
                        pass

            counter_tier = combat_tiers.bodyguard_counter_attack_tier()
            for attacker_id in attackers:
                attacker = await game.get_member_safe(guild, attacker_id)
                if combat_tiers.night_attack_would_kill(
                    game,
                    attacker_id,
                    counter_tier,
                    healed=attacker_id in healed_by_map,
                ):
                    if attacker and game.player_roles.get(attacker_id) == "Pirate":
                        from messages import tos as tos_msg

                        act = game.night_actions.get(attacker_id, {})
                        if act.get("type") == "plunder" and act.get("duel_won", False):
                            try:
                                await attacker.send(tos_msg.pirate_bg_duel_blocked())
                            except discord.HTTPException:
                                pass
                        else:
                            try:
                                await attacker.send(tos_msg.killed_by_bodyguard())
                            except discord.HTTPException:
                                pass
                    elif attacker:
                        from messages import tos as tos_msg

                        try:
                            await attacker.send(tos_msg.killed_by_bodyguard())
                        except discord.HTTPException:
                            pass
                    deaths.add(attacker_id)
                    game.night_death_causes[attacker_id] = "bodyguard"
                elif attacker:
                    from messages import tos as tos_msg

                    try:
                        await attacker.send(tos_msg.defense_too_strong())
                    except discord.HTTPException:
                        pass
        else:
            kill_targets.add(protected_target)

    for target_id in kill_targets:
        if target_id in deaths:
            continue
        attack_tier = combat_tiers.max_attack_tier_for_target(attempted_kills, target_id)
        healed = target_id in healed_by_map
        if not combat_tiers.normal_night_attack_lethal(
            game, target_id, attack_tier, healed=healed
        ):
            combat_tiers.record_non_lethal_kill_outcome(
                game, target_id, attack_tier, healed=healed
            )
            continue
        deaths.add(target_id)
        if target_id not in game.night_death_causes:
            primary = combat_tiers.primary_kill_attacker_for_target(
                attempted_kills, target_id
            )
            if primary is not None:
                _pk_actor, pk_type = primary
                game.night_death_causes[target_id] = (
                    combat_tiers.night_death_cause_for_action(pk_type)
                )

    alert_tier = combat_tiers.scary_grandma_alert_attack_tier()
    alert_sg_survived_dms: List[Tuple[int, int]] = []
    for tgt in alert_visitor_targets:
        if tgt in deaths:
            continue
        tgt_healed = tgt in healed_by_map
        if not combat_tiers.night_attack_would_kill(
            game, tgt, alert_tier, healed=tgt_healed
        ):
            combat_tiers.set_survived_attack_reason(
                game, tgt, alert_tier, healed=tgt_healed
            )
            for sg_id, state in game.role_states.items():
                if sg_id not in living_ids_set or not state.get("is_on_alert"):
                    continue
                if tgt in visit_log.get(int(sg_id), []):
                    alert_sg_survived_dms.append((int(sg_id), int(tgt)))
            continue
        deaths.add(tgt)
        game.night_death_causes.setdefault(tgt, "scary_grandma")

    for sg_id, _visitor_id in alert_sg_survived_dms:
        sg_member = await game.get_member_safe(guild, sg_id)
        if sg_member:
            from messages import tos as tos_msg

            await _dm_player(sg_member, tos_msg.scary_grandma_alert_visitor_survived())

    for tgt in ignite_deaths:
        healer_id = healed_by_map.get(tgt)
        if healer_id is not None:
            healed_but_died_to_ignite.append((healer_id, tgt))

    for actor_id, target_id, a_type in attempted_kills:
        if actor_id in blocked:
            continue
        from faction_taxonomy import triggers_vig_guilt_on_kill

        if (
            a_type == "shoot"
            and target_id in deaths
            and triggers_vig_guilt_on_kill(game.player_roles.get(target_id) or "")
        ):
            actor_role = game.player_roles.get(actor_id)
            shoot_action = game.night_actions.get(actor_id) or {}
            vig_corpse_shot = False
            if actor_role == "Retributionist" and shoot_action.get("_from_retri") is not None:
                from reanimate_expand import graveyard_real_role_for_corpse

                vig_corpse_shot = (
                    graveyard_real_role_for_corpse(game, shoot_action.get("_from_retri")) == "Vigilante"
                )
            if actor_role == "Vigilante" or vig_corpse_shot:
                game.role_states.setdefault(actor_id, {})["guilty_tomorrow"] = True
                attacker = await game.get_member_safe(guild, actor_id)
                if attacker:
                    from messages import tos as tos_msg

                    try:
                        await _dm_player(attacker, tos_msg.vig_guilt_private_warning())
                    except discord.HTTPException:
                        pass
        if target_id in deaths:
            continue
        if actor_id in deaths:
            continue

        attack_tier = combat_tiers.attack_tier_for_night_action(a_type)
        healed = target_id in healed_by_map
        defended = combat_tiers.normal_night_attack_blocked(
            game,
            target_id,
            attack_tier,
            healed=healed,
            for_attacker_feedback=True,
        )
        if defended:
            attacker = await game.get_member_safe(guild, actor_id)
            if attacker:
                try:
                    if game.role_states.get(target_id, {}).get("ga_shield_active_tonight"):
                        from messages import tos as tos_msg

                        await attacker.send(tos_msg.ga_ward_blocked_attacker())
                    else:
                        from messages import tos as tos_msg

                        await _dm_player(attacker, tos_msg.defense_too_strong())
                except discord.HTTPException:
                    pass

    for healer_id, dead_tgt in healed_but_died_to_ignite:
        if healer_id in blocked:
            continue
        from messages import tos as tos_msg

        doctor = await game.get_member_safe(guild, healer_id)
        await _dm_player(doctor, tos_msg.doctor_heal_unstoppable_no_effect())

    if ignite_killers and ignite_deaths:
        for killer_id in ignite_killers:
            arso = await game.get_member_safe(guild, killer_id)
            if not arso:
                continue
            for dead_id in ignite_deaths:
                st = game.role_states.get(dead_id, {}) or {}
                had_night_def = bool(
                    st.get("is_on_alert")
                    or st.get("is_vested")
                    or dead_id in healed_by_map
                )
                if not had_night_def:
                    continue
                from messages import tos as tos_msg

                try:
                    await arso.send(tos_msg.arso_ignited_through_defense())
                except discord.HTTPException:
                    pass

    for pid in list(game.night_death_causes):
        if pid not in deaths:
            del game.night_death_causes[pid]

    return deaths

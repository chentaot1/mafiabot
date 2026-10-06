"""AI night action selection (competence heuristics)."""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Set, Tuple

from scripts.monte_carlo.config import (
    CONTROL_IMMUNE,
    Action,
    REALISTIC_N1_VIG_SHOOT,
    REALISTIC_NIGHT_ACTIONS,
    SimStats,
    _competence_for_axis,
    _pick_with_competence,
    mc_roll,
)


def _act_roll(base_probability: float) -> bool:
    """Return True if the role should act tonight (always when realistic nights are on)."""
    if REALISTIC_NIGHT_ACTIONS:
        return True
    return mc_roll(base_probability)
from scripts.monte_carlo.state import (
    Player,
    _mafia_killer_id,
    _seer_bucket_for_player,
    first_living_with_role,
)
from scripts.monte_carlo.state import (
    arsonist_should_ignite,
    doctor_heal,
    mafia_choose_kill,
    pick_alive,
    pick_chaos_pair,
    pick_investigation_target,
    pick_mole_reveal_target,
    pick_priority_role_target,
    pick_seer_gaze_pair,
    pick_track_target,
    pick_transporter_pair,
    pick_watch_target,
    pirate_duel_won,
    pirate_pick_plunder_target,
    scary_grandma_should_alert,
    survivor_vest_if_needed,
    town_lynch_decision,
    town_lynch_decision_desperate,
)


def generate_night_actions(
    players: List[Player],
    alive: Set[int],
    evidence: Dict[int, int],
    doused: Set[int],
    dead_town_corpses: List[Tuple[int, str]],
    used_corpse_ids: Set[int],
    day: int,
    *,
    collect_stats: bool,
    stats: SimStats,
    trace: bool,
    log: List[str],
) -> list[Action]:
    actions: list[Action] = []
    # Survivors may vest.
    for pid in list(alive):
        if players[pid].role == "Survivor" and survivor_vest_if_needed(players, pid):
            actions.append({"type": "vest", "actor": pid, "role": "Survivor"})
            if trace:
                log.append(f"Survivor P{pid} uses vest.")

    # Transporter (swap two every night while alive — sim uses full action rate)
    for pid in list(alive):
        if players[pid].role == "Transporter":
            good_a, good_b, rnd_a, rnd_b = pick_transporter_pair(players, alive, pid)
            a = _pick_with_competence(
                competence=_competence_for_axis("Transporter", "targeting"),
                good_choice=good_a,
                random_choice=rnd_a,
            )
            b = _pick_with_competence(
                competence=_competence_for_axis("Transporter", "targeting"),
                good_choice=good_b,
                random_choice=rnd_b,
            )
            if a is not None and b is not None and a != b:
                actions.append({"type": "transport", "actor": pid, "targets": [a, b], "role": "Transporter"})
                if trace:
                    log.append(f"Transporter P{pid} transports P{a}<->P{b}.")
            break

    # Witch control (redirect a controlled player's target to another)
    witch_id = first_living_with_role(players, alive, "Witch")
    controlled_id: Optional[int] = None
    control_target: Optional[int] = None
    if witch_id is not None and _act_roll(0.35):
        controlled_id = pick_alive(players, alive, exclude={witch_id})
        control_target = pick_alive(players, alive, exclude={witch_id, controlled_id} if controlled_id is not None else {witch_id})
        if controlled_id is not None and control_target is not None:
            if players[controlled_id].role not in CONTROL_IMMUNE:
                good_ctrl = town_lynch_decision(players, alive, evidence) or controlled_id
                good_tgt = mafia_choose_kill(players, alive, witch_id) or control_target
                rnd_ctrl = controlled_id
                rnd_tgt = control_target
                controlled_id = _pick_with_competence(
                    competence=_competence_for_axis("Witch", "targeting"),
                    good_choice=good_ctrl if good_ctrl != witch_id else controlled_id,
                    random_choice=rnd_ctrl,
                )
                control_target = _pick_with_competence(
                    competence=_competence_for_axis("Witch", "targeting"),
                    good_choice=good_tgt,
                    random_choice=rnd_tgt,
                )
                if controlled_id is not None and control_target is not None:
                    actions.append({"type": "control", "actor": witch_id, "targets": [controlled_id, control_target], "role": "Witch"})
                    if collect_stats:
                        stats["controls"] += 1
                    if trace:
                        log.append(f"Witch P{witch_id} controls P{controlled_id} -> P{control_target}.")

    # Roleblocks + Gatekeeper guard
    for pid in list(alive):
        r = players[pid].role
        if r == "Escort":
            good = town_lynch_decision_desperate(players, alive, evidence)
            if good is None or good == pid:
                good = pick_alive(players, alive, exclude={pid})
            rnd = pick_alive(players, alive, exclude={pid})
            tgt = _pick_with_competence(
                competence=_competence_for_axis("Escort", "targeting"),
                good_choice=good,
                random_choice=rnd,
            )
            if tgt is not None:
                actions.append({"type": "roleblock", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Escort P{pid} roleblocks P{tgt}.")
        elif r == "Consort":
            tgt = pick_priority_role_target(
                players,
                alive,
                pid,
                "Consort",
                ("Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Vigilante", "Transporter"),
            )
            if tgt is not None:
                actions.append({"type": "roleblock", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Consort P{pid} roleblocks P{tgt}.")
        elif r == "Gatekeeper" and players[pid].gatekeeper_uses_left > 0:
            tgt = pick_priority_role_target(
                players,
                alive,
                pid,
                "Gatekeeper",
                ("Sheriff", "Investigator", "Doctor", "Mayor"),
            )
            if tgt is not None:
                actions.append({"type": "guard", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Gatekeeper P{pid} guards P{tgt}.")

    # Frames / heals / protects / alerts / tailor / gravedigger / hypnotist / arsonist
    for pid in list(alive):
        r = players[pid].role
        if r == "Framer":
            # Bot: only Nights 1 and 2.
            if day > 2:
                continue
            tgt = None
            for prefer in ("Sheriff", "Investigator", "Lookout", "Tracker"):
                tgt = first_living_with_role(players, alive, prefer)
                if tgt is not None:
                    break
            good = tgt if tgt is not None else pick_alive(players, alive, exclude={pid})
            rnd = pick_alive(players, alive, exclude={pid})
            tgt = _pick_with_competence(
                competence=_competence_for_axis("Framer", "targeting"),
                good_choice=good,
                random_choice=rnd,
            )
            if tgt is not None:
                actions.append({"type": "frame", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Framer P{pid} frames P{tgt}.")
        elif r == "Doctor":
            tgt = doctor_heal(players, alive, pid)
            if tgt is not None:
                actions.append({"type": "heal", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Doctor P{pid} heals P{tgt}.")
        elif r == "Bodyguard":
            if players[pid].bg_self_protects_left > 0 and sum(1 for x in players if x.alive) <= 5 and mc_roll(0.2):
                actions.append({"type": "bg_vest", "actor": pid, "role": r})
                if trace:
                    log.append(f"Bodyguard P{pid} uses vest.")
            elif players[pid].bg_uses_left > 0:
                tgt = pick_priority_role_target(
                    players,
                    alive,
                    pid,
                    "Bodyguard",
                    ("Sheriff", "Investigator", "Doctor", "Mayor", "Lookout", "Tracker"),
                )
                if tgt is not None:
                    actions.append({"type": "protect", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Bodyguard P{pid} protects P{tgt}.")
        elif r == "Scary Grandma" and players[pid].alerts_left > 0:
            if scary_grandma_should_alert(players):
                actions.append({"type": "alert", "actor": pid, "role": r})
                if trace:
                    log.append(f"Scary Grandma P{pid} goes on alert.")
        elif r == "Tailor" and players[pid].tailor_uses_left > 0 and _act_roll(0.35):
            tgt = pick_alive(players, alive, exclude={pid})
            if tgt is not None:
                fake = random.choice(["Sheriff", "Investigator", "Lookout", "Tracker", "Doctor", "Retributionist", "Mobster"])
                actions.append({"type": "tailor", "actor": pid, "target": tgt, "fake_role": fake, "role": r})
        elif r == "Gravedigger" and players[pid].gravedigger_uses_left > 0 and _act_roll(0.35):
            tgt = pick_alive(players, alive, exclude={pid})
            if tgt is not None:
                actions.append({"type": "hide", "actor": pid, "target": tgt, "role": r})
        elif r == "Hypnotist" and _act_roll(0.4):
            tgt = pick_alive(players, alive, exclude={pid})
            if tgt is not None:
                msg = random.choice(["healed", "roleblocked", "transported", "controlled", "attacked"])
                actions.append({"type": "hypnotize", "actor": pid, "target": tgt, "msg_type": msg, "role": r})
        elif r == "Arsonist":
            # ToS-like quirk: if the Arsonist is doused, they should typically spend the night cleaning,
            # otherwise igniting can kill them too.
            if pid in doused:
                actions.append({"type": "clean", "actor": pid, "role": r})
                if trace:
                    log.append(f"Arsonist P{pid} cleans gasoline off themselves.")
                continue

            # Avoid artificial stalemates: don't ignite unless at least one living player is doused.
            # In particular, in a 1v1 vs Mafia, Arsonist should douse first, then ignite to win.
            others_alive = [x for x in alive if x != pid]
            living_doused = [x for x in others_alive if x in doused]

            if len(others_alive) == 1:
                only_other = others_alive[0]
                if only_other in doused:
                    actions.append({"type": "ignite", "actor": pid, "role": r})
                    if trace:
                        log.append(f"Arsonist P{pid} chooses IGNITE.")
                else:
                    actions.append({"type": "douse", "actor": pid, "target": only_other, "role": r})
                    if trace:
                        log.append(f"Arsonist P{pid} douses P{only_other}.")
            else:
                if arsonist_should_ignite(
                    living_doused=living_doused,
                    others_alive=len(others_alive),
                    total_doused=len(doused),
                ):
                    actions.append({"type": "ignite", "actor": pid, "role": r})
                    if trace:
                        log.append(f"Arsonist P{pid} chooses IGNITE.")
                else:
                    good = mafia_choose_kill(players, alive, pid)
                    rnd = pick_alive(players, alive, exclude={pid})
                    tgt = _pick_with_competence(
                        competence=_competence_for_axis("Arsonist", "targeting"),
                        good_choice=good,
                        random_choice=rnd,
                    )
                    if tgt is not None:
                        actions.append({"type": "douse", "actor": pid, "target": tgt, "role": r})
                        if trace:
                            log.append(f"Arsonist P{pid} douses P{tgt}.")
        elif r == "Chaos" and players[pid].chaos_uses_left > 0 and mc_roll(0.75):
            good1, good2, rnd1, rnd2 = pick_chaos_pair(players, alive, pid)
            t1 = _pick_with_competence(
                competence=_competence_for_axis("Chaos", "targeting"),
                good_choice=good1,
                random_choice=rnd1,
            )
            t2 = _pick_with_competence(
                competence=_competence_for_axis("Chaos", "targeting"),
                good_choice=good2,
                random_choice=rnd2,
            )
            if t1 is not None and t2 is not None and t1 != t2:
                actions.append({"type": "chaos", "actor": pid, "targets": [t1, t2], "role": r})
                if trace:
                    log.append(f"Chaos P{pid} targets P{t1} and P{t2} (effect resolved by engine).")

    # Retributionist: 2 uses total; reanimate a dead Town corpse to perform its ability.
    # We implement a simple policy: use corpses after day 1, prefer impactful roles.
    for pid in list(alive):
        if players[pid].role != "Retributionist":
            continue
        if players[pid].retri_uses_left <= 0:
            continue
        if day < 2:
            continue
        # pick an unused corpse (prefer Vigi/BG/Doctor/investigatives/escort)
        from reanimate_expand import RETRI_CORPSE_EXPANDABLE_ROLES

        candidates = [
            (c_pid, c_role)
            for (c_pid, c_role) in dead_town_corpses
            if c_pid not in used_corpse_ids and c_role in RETRI_CORPSE_EXPANDABLE_ROLES
        ]
        if not candidates:
            continue

        pref_order = [
            "Vigilante",
            "Bodyguard",
            "Doctor",
            "Sheriff",
            "Investigator",
            "Lookout",
            "Tracker",
            "Escort",
        ]
        candidates.sort(key=lambda cr: pref_order.index(cr[1]) if cr[1] in pref_order else 999)
        good_corpse = candidates[0]
        rnd_corpse = random.choice(candidates) if candidates else good_corpse
        corpse_pid, corpse_role = (
            good_corpse if mc_roll(_competence_for_axis("Retributionist", "usage")) else rnd_corpse
        )

        # Choose targets
        t1_good = pick_alive(players, alive, exclude={pid})
        t1_rnd = pick_alive(players, alive, exclude={pid})
        t1 = _pick_with_competence(
            competence=_competence_for_axis("Retributionist", "targeting"),
            good_choice=t1_good,
            random_choice=t1_rnd,
        )
        if t1 is None:
            continue
        if corpse_role == "Doctor":
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Doctor -> heal P{t1}.")
        elif corpse_role in {"Sheriff", "Investigator"}:
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates {corpse_role} -> investigate P{t1}.")
        elif corpse_role == "Lookout":
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Lookout -> watch P{t1}.")
        elif corpse_role == "Tracker":
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Tracker -> track P{t1}.")
        elif corpse_role == "Escort":
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Escort -> roleblock P{t1}.")
        elif corpse_role == "Vigilante":
            # Live engine applies guilt to the Retributionist when the corpse Vig kills Town (CR32).
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Vigilante -> shoot P{t1}.")
        elif corpse_role == "Bodyguard":
            # Ret using BG corpse: protect and counterkill attacker, but Ret does not die.
            actions.append({"type": "reanimate", "actor": pid, "corpse_role": corpse_role, "corpse_player_id": corpse_pid, "target": t1, "role": "Retributionist"})
            if trace:
                log.append(f"Retributionist P{pid} reanimates Bodyguard -> protect P{t1} (ret_protect).")

    # Investigations / watch / track
    for pid in list(alive):
        r = players[pid].role
        if r in {"Sheriff", "Investigator"}:
            tgt = pick_investigation_target(players, alive, pid, evidence, r)
            if tgt is not None:
                actions.append({"type": "investigate", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"{r} P{pid} investigates P{tgt}.")
        elif r == "Mole" and players[pid].mole_uses_left > 0:
            tgt = pick_mole_reveal_target(players, alive, pid, evidence)
            if tgt is not None:
                actions.append({"type": "investigate", "actor": pid, "target": tgt, "role": r})
                if trace:
                    log.append(f"Mole P{pid} investigates P{tgt} (reveal role).")
        elif r == "Lookout":
            watch_pid = pick_watch_target(players, alive, pid)
            actions.append({"type": "watch", "actor": pid, "target": watch_pid, "role": r})
            if trace:
                log.append(f"Lookout P{pid} watches P{watch_pid}.")
        elif r == "Tracker":
            track_pid = pick_track_target(players, alive, pid, evidence)
            actions.append({"type": "track", "actor": pid, "target": track_pid, "role": r})
            if trace:
                log.append(f"Tracker P{pid} tracks P{track_pid}.")

    # Seer gaze (Friends/Enemies — evidence bump on enemies)
    for pid in list(alive):
        if players[pid].role != "Seer":
            continue
        pair = pick_seer_gaze_pair(
            players,
            alive,
            pid,
            doused,
            used_pairs=players[pid].seer_gazed_pairs,
        )
        if pair is None:
            continue
        t1, t2 = pair
        players[pid].seer_gazed_pairs.add(frozenset({t1, t2}))
        actions.append({"type": "gaze", "actor": pid, "targets": [t1, t2], "role": "Seer"})
        ba = _seer_bucket_for_player(players, t1, doused=doused)
        bb = _seer_bucket_for_player(players, t2, doused=doused)
        if ba == 4 or bb == 4 or ba != bb:
            evidence[t1] = evidence.get(t1, 0) + 1
            evidence[t2] = evidence.get(t2, 0) + 1
        if trace:
            log.append(f"Seer P{pid} gazes at P{t1} and P{t2}.")

    # Guardian Angel ward (bind only; 1 charge; living physical or graveyard astral)
    ga_ward_pids = [pid for pid in alive if players[pid].role == "Guardian Angel"]
    for pid in range(len(players)):
        if pid in alive:
            continue
        ga = players[pid]
        if ga.role != "Guardian Angel" or ga.ga_defeated or ga.ga_ward_charges <= 0:
            continue
        ga_ward_pids.append(pid)
    for pid in ga_ward_pids:
        ga = players[pid]
        if ga.ga_defeated or ga.ga_ward_charges <= 0:
            continue
        if ga.ga_bind_id is not None and ga.ga_bind_id in alive:
            actions.append({"type": "ward", "actor": pid, "target": ga.ga_bind_id, "role": "Guardian Angel"})

    # Serial Killer: occasional !cautious toggle (Aggressive vs Cautious)
    for pid in list(alive):
        if players[pid].role != "Serial Killer":
            continue
        if mc_roll(0.12):
            players[pid].sk_cautious = not players[pid].sk_cautious
            if trace:
                mode = "Cautious" if players[pid].sk_cautious else "Aggressive"
                log.append(f"Serial Killer P{pid} toggles to {mode}.")

    # Serial Killer primary stab
    for pid in list(alive):
        if players[pid].role != "Serial Killer":
            continue
        if players[pid].sk_suppressed_by_pirate:
            continue
        tgt = mafia_choose_kill(players, alive, pid)
        if tgt is not None:
            actions.append({"type": "sk_kill", "actor": pid, "target": tgt, "role": "Serial Killer"})

    # Mafia kill (Mobster only; matches bot night_factory)
    mafia_killer_id = _mafia_killer_id(players, alive)
    if mafia_killer_id is not None:
        tgt = mafia_choose_kill(players, alive, mafia_killer_id)
        if tgt is not None:
            actions.append(
                {
                    "type": "kill",
                    "actor": mafia_killer_id,
                    "target": tgt,
                    "role": "Mobster",
                }
            )
            if trace:
                log.append(
                    f"Mobster P{mafia_killer_id} kills P{tgt} "
                    f"(engine applies transport/Witch redirects)."
                )

    # Vigilante: N1 shoot 10% chance; from N2 onward always shoot while ammo remains
    for pid in list(alive):
        if players[pid].role != "Vigilante" or players[pid].shots_left <= 0:
            continue
        if day == 1 and not REALISTIC_N1_VIG_SHOOT and not mc_roll(0.10):
            continue
        good = town_lynch_decision(players, alive, evidence)
        if good is None or good == pid:
            good = pick_alive(players, alive, exclude={pid})
        rnd = pick_alive(players, alive, exclude={pid})
        tgt = _pick_with_competence(
            competence=_competence_for_axis("Vigilante", "targeting"),
            good_choice=good,
            random_choice=rnd,
        )
        if tgt is not None and tgt != pid:
            actions.append({"type": "shoot", "actor": pid, "target": tgt, "role": "Vigilante"})
            if trace:
                if day == 1:
                    log.append(f"Vigilante P{pid} shoots P{tgt} (N1, 10% sim policy).")
                else:
                    log.append(f"Vigilante P{pid} shoots P{tgt}.")

    # Pirate plunder
    pirate_id = first_living_with_role(players, alive, "Pirate")
    if pirate_id is not None:
        tgt = pirate_pick_plunder_target(players, alive, pirate_id)
        if tgt is not None:
            duel_won = pirate_duel_won()
            actions.append(
                {
                    "type": "plunder",
                    "actor": pirate_id,
                    "target": tgt,
                    "role": "Pirate",
                    "duel_won": duel_won,
                    "duel_finished": True,
                }
            )
            if trace:
                log.append(
                    f"Pirate P{pirate_id} plunders P{tgt} (duel_won={duel_won}; win only if plunder kill lands)."
                )

    return actions

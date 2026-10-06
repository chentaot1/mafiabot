"""Single-game Monte Carlo simulation loop."""
from __future__ import annotations

import random
from typing import Dict, List, Set, Tuple, cast

from scripts.monte_carlo import bridge as monte_bridge
from scripts.monte_carlo.config import ALL_ROLES, MAFIA, NEUTRAL, SimStats, TOWN, _competence_for_role
from scripts.monte_carlo.day import (
    DayPhaseState,
    PendingJesterHaunt,
    deputy_pick_target,
    deputy_should_shoot_today,
    infer_eligible_haunt_voters,
    mayor_may_reveal,
    pick_lynch_defendant,
    reset_day_phase,
)
from scripts.monte_carlo.night_ai import generate_night_actions
from scripts.monte_carlo.state import (
    Player,
    _alive_ids,
    _init_role_state,
    _town_competence,
    check_main_winner,
    faction,
)
from scripts.monte_carlo.wins import apply_personal_win_flags, arsonist_stalemate_win, maybe_promote_mobster, try_two_player_stalemate_end


def _try_stalemate_draw_or_override(players: List[Player], alive: Set[int], out: Dict[str, bool]) -> None:
    """Record true Draw or ToS1 personal override (mirrors game.py stalemate end)."""
    from draw_override_wins import apply_stalemate_draw_override, role_states_from_mc_players

    player_roles = {p.i: p.role for p in players}
    role_states = role_states_from_mc_players(players)
    if apply_stalemate_draw_override(player_roles, role_states, alive, out) is None:
        out["Draw"] = True


def _resolve_main_winner(players: List[Player], alive: Set[int], out: Dict[str, bool]) -> bool:
    """Apply check_main_winner; handle wipeout Draw override. Returns True if sim should stop."""
    w = check_main_winner(players)
    if not w:
        return False
    if w == "Draw":
        _try_stalemate_draw_or_override(players, alive, out)
    else:
        out[w] = True
    return True


def simulate_once(
    roles: List[str],
    *,
    max_days: int = 20,
    collect_stats: bool = False,
    trace: bool = False,
) -> Dict[str, bool] | Tuple[Dict[str, bool], SimStats] | Tuple[Dict[str, bool], List[str]]:
    unknown_roles = [r for r in roles if r not in ALL_ROLES]
    if unknown_roles:
        raise ValueError(
            f"Unknown role(s) {unknown_roles!r}. Valid roles: {sorted(ALL_ROLES)}"
        )

    players = [Player(i=i, role=r) for i, r in enumerate(roles)]
    player_count = len(roles)
    for p in players:
        _init_role_state(p, player_count=player_count)

    alive: Set[int] = {p.i for p in players}
    evidence: Dict[int, int] = {}  # suspicion points
    doused: Set[int] = set()  # Arsonist global douse list
    # Graveyard bookkeeping for Retributionist (simplified):
    # store dead Town roles and allow each corpse to be used once.
    dead_town_corpses: List[Tuple[int, str]] = []  # (pid, role)
    used_corpse_ids: Set[int] = set()

    # Assign Executioner target (Town non-Mayor).
    exe_ids = [p.i for p in players if p.role == "Executioner"]
    if exe_ids:
        exe_id = exe_ids[0]
        town_targets = [p.i for p in players if p.i != exe_id and p.role in TOWN and p.role != "Mayor"]
        if town_targets:
            players[exe_id].exe_target = random.choice(town_targets)
        else:
            players[exe_id].role = "Jester"

    import config as bot_config

    for ga in players:
        if ga.role != "Guardian Angel":
            continue
        bind_candidates = bot_config.guardian_angel_bind_pool_ids([p.i for p in players], ga.i)
        ga.ga_bind_id = random.choice(bind_candidates) if bind_candidates else None
        ga.ga_ward_charges = 1

    # Outcome flags (multiple can be true).
    out: Dict[str, bool] = {
        "Town": False,
        "Mafia": False,
        "Draw": False,
        "Executioner": False,
        "Jester": False,
        "Survivor": False,
        "Guardian Angel": False,
        "Pirate": False,
        "Arsonist": False,
        "Chaos": False,
        "Serial Killer": False,
        "Witch": False,
    }

    day = 1
    no_lynch_streak = 0
    bloodless_cycle_streak = 0
    deaths_this_cycle = 0
    day_phase = DayPhaseState()
    pending_jester_haunts: List[PendingJesterHaunt] = []
    hidden_corpse_ids: Set[int] = set()
    log: List[str] = []
    if trace:
        log.append("=== TRACE START ===")
        log.append("Roles by seat:")
        for p in players:
            log.append(f"  P{p.i}: {p.role}")
        for p in players:
            if p.role == "Guardian Angel" and p.ga_bind_id is not None:
                bind_role = next((x.role for x in players if x.i == p.ga_bind_id), "?")
                log.append(f"  GA P{p.i} bind -> P{p.ga_bind_id} ({bind_role})")
    stats: SimStats = {}
    if collect_stats:
        stats = {
            "days": 0,
            "lynches": 0,
            "mislynches": 0,
            "mislynches_incl_neutrals": 0,
            "lynches_neutral": 0,
            "night_deaths": 0,
            "doc_saves": 0,
            "roleblocks": 0,
            "gatekeeper_blocks": 0,
            "controls": 0,
            "controls_prevent_ignite": 0,
            "ignites": 0,
        }
    while day <= max_days:
        if day > 1:
            if deaths_this_cycle <= 0:
                bloodless_cycle_streak += 1
            else:
                bloodless_cycle_streak = 0
            deaths_this_cycle = 0
            if bloodless_cycle_streak >= bot_config.STALEMATE_DRAW_CYCLES:
                _try_stalemate_draw_or_override(players, alive, out)
                break

        # Bot resets deputy_revolver_fired at start_night (game.py).
        day_phase.deputy_revolver_fired = False

        # ToS following-night guilt (game.start_night ``_process_deferred_guilt_at_night_start``).
        for pid in list(alive):
            if not players[pid].guilty_next_day:
                continue
            players[pid].guilty_next_day = False
            players[pid].alive = False
            alive.discard(pid)
            deaths_this_cycle += 1
            if collect_stats:
                stats["night_deaths"] += 1
            if trace:
                log.append(f"Guilt suicide: P{pid} ({players[pid].role})")

        # Reset night flags
        for pid in list(alive):
            p = players[pid]
            p.framed_tonight = False
            p.roleblocked_tonight = False
            p.protected_tonight = False
            p.on_alert_tonight = False
            p.transported_with = None

        if trace:
            log.append(f"\n--- Night {day} ---")

        actions = generate_night_actions(
            players,
            alive,
            evidence,
            doused,
            dead_town_corpses,
            used_corpse_ids,
            day,
            collect_stats=collect_stats,
            stats=stats,
            trace=trace,
            log=log,
        )

        # --- Night resolution: live engine (engine/night.run_night_pipeline) ---
        game, guild = monte_bridge.build_game_from_sim(
            players,
            alive,
            doused=doused,
            dead_town_corpses=dead_town_corpses,
            used_corpse_ids=used_corpse_ids,
            hidden_corpse_ids=hidden_corpse_ids,
            day=day,
        )
        game.night_actions = monte_bridge.actions_to_night_actions(actions)
        deaths_set, blocked, stat_deltas, death_trace = monte_bridge.resolve_night_via_engine(
            game, guild, evidence=evidence
        )
        if trace and death_trace:
            log.extend(death_trace)

        if pending_jester_haunts:
            haunt_deaths = monte_bridge.run_async(
                monte_bridge.resolve_jester_haunts(game, guild, pending_jester_haunts)
            )
            pending_jester_haunts.clear()
            for pid in haunt_deaths:
                if pid in alive:
                    if collect_stats:
                        stats["night_deaths"] += 1
                    deaths_this_cycle += 1
                    players[pid].alive = False
                    alive.discard(pid)
                    if trace:
                        log.append(f"Jester haunt death: P{pid} ({players[pid].role})")
            deaths_set |= haunt_deaths

        monte_bridge.run_async(monte_bridge.sweep_executioner_conversions(game, guild, deaths_set))
        monte_bridge.sync_engine_to_sim(game, players, alive, doused=doused, used_corpse_ids=used_corpse_ids)
        blocked_set = set(blocked)
        if trace and blocked_set:
            log.append(
                'Blocked tonight: '
                + ', '.join(f'P{pid}({players[pid].role})' for pid in sorted(blocked_set) if pid in alive)
            )

        if collect_stats:
            stats['roleblocks'] += stat_deltas.get('roleblocks', 0)
            stats['gatekeeper_blocks'] += stat_deltas.get('gatekeeper_blocks', 0)
            stats['ignites'] += stat_deltas.get('ignites', 0)

        if collect_stats and stat_deltas.get("doc_saves"):
            stats["doc_saves"] += int(stat_deltas["doc_saves"])

        for pid in sorted(deaths_set):
            if pid not in alive:
                continue
            hidden_corpse = bool(game.role_states.get(pid, {}).get('is_hidden_by_gravedigger'))
            if hidden_corpse:
                hidden_corpse_ids.add(pid)
            if collect_stats:
                stats['night_deaths'] += 1
            deaths_this_cycle += 1
            players[pid].alive = False
            alive.discard(pid)
            if trace:
                log.append(f'Night death: P{pid} ({players[pid].role})')
            if players[pid].role in TOWN and not hidden_corpse:
                dead_town_corpses.append((pid, players[pid].role))
            for ga in players:
                if ga.role == 'Guardian Angel' and ga.ga_bind_id == pid:
                    ga.ga_defeated = True

        for p in players:
            if p.alive and p.role == 'Pirate' and p.pirate_wins >= 2:
                out['Pirate'] = True

        # Arsonist win checks (mirror game.py precedence: check Arsonist before faction wins).
        alive_ids_now = _alive_ids(players)
        arso_ids_now = [pid for pid in alive_ids_now if players[pid].role == 'Arsonist']
        if arso_ids_now:
            arso_id = arso_ids_now[0]
            if len(alive_ids_now) == 1:
                out['Arsonist'] = True
                break
            if try_two_player_stalemate_end(players, alive, out):
                break
            if arsonist_stalemate_win(players, alive):
                out['Arsonist'] = True
                break

        # ---- Day: update evidence based on night info ----
        if trace:
            alive_list = ", ".join(f"P{pid}({players[pid].role})" for pid in sorted(alive))
            log.append(f"\n--- Day {day} ---")
            log.append(f"Alive: {alive_list}")
        reset_day_phase(day_phase)
        mayor_id = next((pid for pid in sorted(alive) if players[pid].role == "Mayor"), None)
        if mayor_id is not None and mayor_may_reveal(players, mayor_id, alive, evidence, day):
            players[mayor_id].mayor_revealed = True
            if trace:
                log.append(f"Mayor P{mayor_id} reveals (double vote).")

        # Executioner pressure: tries to get their target lynched by pushing suspicion daily.
        for p in players:
            if p.alive and p.role == "Executioner" and p.exe_target is not None and p.exe_target in alive:
                evidence[p.exe_target] = evidence.get(p.exe_target, 0) + 1

        # Jester behavior: acts scummy / draws heat (increases odds of being lynched).
        for p in players:
            if p.alive and p.role == "Jester":
                evidence[p.i] = evidence.get(p.i, 0) + 1

        # (All investigative evidence + night kills are applied during the night pipeline above.)

        # Check win after daybreak deaths/shots.
        alive_ids_now = _alive_ids(players)
        if len(alive_ids_now) == 1 and players[next(iter(alive_ids_now))].role == "Serial Killer":
            out["Serial Killer"] = True
            break

        maybe_promote_mobster(players, alive, trace=trace, log=log)
        if _resolve_main_winner(players, alive, out):
            break

        # Vigilante guilt is applied at night via ``night_guilt.tally`` (live ``!resolve`` parity).

        # Deputy daytime revolver (bot: day 2+, per-Deputy once per day, not during vote_in_progress)
        if not day_phase.vote_in_progress:
            deputy_ids = sorted(
                pid
                for pid in alive
                if players[pid].role == "Deputy" and players[pid].deputy_shots_remaining > 0
            )
            for deputy_id in deputy_ids:
                dep_game, dep_guild = monte_bridge.build_game_from_sim(
                    players,
                    alive,
                    doused=doused,
                    dead_town_corpses=dead_town_corpses,
                    used_corpse_ids=used_corpse_ids,
                    hidden_corpse_ids=hidden_corpse_ids,
                    day=day,
                )
                if dep_game.deputy_fired_today(deputy_id):
                    continue
                if not deputy_should_shoot_today(
                    day,
                    already_fired=False,
                    shots_left=players[deputy_id].deputy_shots_remaining,
                ):
                    continue
                tgt = deputy_pick_target(players, alive, deputy_id, evidence)
                if tgt is None or tgt not in alive:
                    continue
                shot_deaths = monte_bridge.run_async(
                    monte_bridge.deputy_day_shot(dep_game, dep_guild, deputy_id, tgt)
                )
                monte_bridge.run_async(
                    monte_bridge.sweep_executioner_conversions(dep_game, dep_guild, shot_deaths)
                )
                monte_bridge.sync_engine_to_sim(
                    dep_game, players, alive, doused=doused, used_corpse_ids=used_corpse_ids
                )
                for pid in shot_deaths:
                    if pid in alive:
                        deaths_this_cycle += 1
                        players[pid].alive = False
                        alive.discard(pid)
                        if trace:
                            log.append(f"Deputy shot death: P{pid} ({players[pid].role})")
                maybe_promote_mobster(players, alive, trace=trace, log=log)
                if _resolve_main_winner(players, alive, out):
                    break

        # ---- Tribunal / lynch (skip day when no suspicion / no clear suspect) ----
        day_phase.vote_in_progress = True
        lynch = pick_lynch_defendant(
            players,
            alive,
            evidence,
            mayor_id=mayor_id,
            no_lynch_streak=no_lynch_streak,
            force=False,
        )
        day_phase.vote_in_progress = False
        if lynch is None:
            no_lynch_streak += 1
        else:
            no_lynch_streak = 0

        if lynch is not None:
            voter_pool = alive.copy()
            deaths_this_cycle += 1
            players[lynch].alive = False
            alive.discard(lynch)
            if trace:
                log.append(f"Lynch: P{lynch} ({players[lynch].role})")
            if collect_stats:
                stats["lynches"] += 1
                lynch_role = players[lynch].role
                lynch_faction = faction(lynch_role)
                if lynch_faction == "Town":
                    stats["mislynches"] += 1
                elif lynch_faction == "Neutral":
                    stats["lynches_neutral"] += 1
                if lynch_faction != "Mafia":
                    stats["mislynches_incl_neutrals"] += 1
            from scripts.monte_carlo.sim_death import apply_ga_bind_death_sim

            apply_ga_bind_death_sim(players, lynch, cause="lynch", day=day)
            if players[lynch].role == "Jester":
                out["Jester"] = True
                players[lynch].jester_won = True
                haunt_eligible = infer_eligible_haunt_voters(
                    players,
                    voter_pool,
                    lynch,
                    evidence,
                    mayor_id=mayor_id,
                    mayor_revealed=mayor_id is not None and players[mayor_id].mayor_revealed,
                )
                pending_jester_haunts.append(
                    PendingJesterHaunt(jester_id=lynch, guilty_voters=haunt_eligible)
                )
                if trace:
                    gv = ", ".join(f"P{g}" for g in haunt_eligible)
                    log.append(f"Jester P{lynch} may haunt eligible voters: {gv}")
            # Executioner wins immediately if their target is lynched while Executioner is alive.
            for p in players:
                if p.alive and p.role == "Executioner" and p.exe_target == lynch:
                    p.exe_won = True
                    out["Executioner"] = True
            # If Town lynched a townie, reduce confidence a bit (but keep simple).
            if faction(players[lynch].role) == "Town":
                # Randomly distribute some uncertainty.
                for pid in list(alive):
                    evidence[pid] = max(0, evidence.get(pid, 0) - 1)

        maybe_promote_mobster(players, alive, trace=trace, log=log)

        if try_two_player_stalemate_end(players, alive, out):
            break

        if _resolve_main_winner(players, alive, out):
            break

        day += 1
        if collect_stats:
            stats["days"] += 1

    if not (out["Town"] or out["Mafia"] or out["Executioner"]):
        if not (out.get("Jester") or out.get("Pirate") or out.get("Arsonist") or out.get("Serial Killer")):
            if not any(out.get(r) for r in ("Survivor", "Chaos", "Guardian Angel")):
                _try_stalemate_draw_or_override(players, alive, out)

    apply_personal_win_flags(players, out)

    if collect_stats:
        return out, stats
    if trace:
        log.append("\nOutcome flags:")
        for k in [
            "Town",
            "Mafia",
            "Executioner",
            "Jester",
            "Survivor",
            "Guardian Angel",
            "Pirate",
            "Arsonist",
            "Chaos",
            "Serial Killer",
            "Witch",
            "Draw",
        ]:
            if out.get(k):
                log.append(f"  {k}=True")
        log.append("=== TRACE END ===")
        return out, log
    return out

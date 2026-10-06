"""Bridge sim state to engine/night.run_night_pipeline (production parity)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set, Tuple

from scripts.monte_carlo.runtime import run_async

from engine.night import (
    ALL_MAFIA_ROLES,
    deliver_psychic_visions,
    investigator_bucket_for,
    run_night_pipeline,
)

import game as game_module
from deputy_rules import _deputy_gun_sees_evil, _deputy_target_basic_defense
from config import PSYCHIC_ODD_EVIL_NEUTRALS
from invariants import assert_post_night_pipeline_invariants
from scripts.monte_carlo import config as mc_config
from scripts.monte_carlo.config import Action
from scripts.monte_carlo.state import Player
from scripts.sim_outcomes import assert_night_outcome_consistency


class _FakeChanCtx:
    """Minimal ctx for Game.process_death_by_id in sim (no Discord channel)."""

    channel = None


class _FakeMember:
    def __init__(self, pid: int, display_name: str | None = None) -> None:
        self.id = int(pid)
        self.display_name = display_name or f"P{pid}"
        self.roles: list = []
        self.guild_permissions = type("Perms", (), {"administrator": False})()

    async def send(self, _msg: str) -> None:
        return None

    async def add_roles(self, *_roles: object) -> None:
        return None

    async def remove_roles(self, *_roles: object) -> None:
        return None


class _FakeGuild:
    _monte_carlo_fake = True

    def __init__(self, members: List[_FakeMember]) -> None:
        self._members = {m.id: m for m in members}

    def get_member(self, uid: int) -> Optional[_FakeMember]:
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int) -> Optional[_FakeMember]:
        return self._members.get(int(uid))

    def get_role(self, _rid: object) -> None:
        return None

    def get_channel(self, _cid: object) -> None:
        return None


def _player_to_role_state(p: Player) -> Dict[str, Any]:
    r = p.role
    st: Dict[str, Any] = {}
    if r == "Survivor":
        st["vests_remaining"] = p.vests_left
    elif r == "Vigilante":
        st["shots_remaining"] = p.shots_left
    elif r == "Doctor":
        st["self_heals_remaining"] = p.self_heals_left
    elif r == "Bodyguard":
        st["uses_remaining"] = p.bg_uses_left
        st["self_protects_remaining"] = p.bg_self_protects_left
    elif r == "Scary Grandma":
        st["alerts_remaining"] = p.alerts_left
    elif r == "Gatekeeper":
        st["uses_remaining"] = p.gatekeeper_uses_left
        if p.gatekeeper_last_guard_target_id is not None:
            st["gatekeeper_last_guard_target_id"] = int(p.gatekeeper_last_guard_target_id)
        if p.gatekeeper_last_successful_guard_day_number is not None:
            st["gatekeeper_last_successful_guard_day_number"] = int(
                p.gatekeeper_last_successful_guard_day_number
            )
    elif r == "Gravedigger":
        st["uses_remaining"] = p.gravedigger_uses_left
    elif r == "Mole":
        st["uses_remaining"] = p.mole_uses_left
    elif r == "Tailor":
        st["uses_remaining"] = p.tailor_uses_left
    elif r == "Retributionist":
        st["uses_remaining"] = p.retri_uses_left
    elif r == "Chaos":
        st["uses_remaining"] = p.chaos_uses_left
        if p.chaos_shield_used:
            st["night1_shield_used"] = True
    elif r == "Deputy":
        st["deputy_shots_remaining"] = p.deputy_shots_remaining
    elif r == "Serial Killer":
        st["sk_cautious"] = p.sk_cautious
        if p.sk_suppressed_by_pirate:
            st["sk_suppressed_by_pirate"] = True
    elif r == "Pirate":
        st["wins"] = p.pirate_wins
    elif r == "Mayor":
        st["is_revealed"] = p.mayor_revealed
    elif r == "Guardian Angel":
        st["ga_ward_charges"] = p.ga_ward_charges
        st["ga_defeated"] = p.ga_defeated
        if p.ga_bind_id is not None:
            st["ga_target_id"] = p.ga_bind_id
    elif r == "Executioner":
        if p.exe_target is not None:
            st["exe_target"] = p.exe_target
        if p.exe_won:
            st["exe_won"] = True
    elif r == "Witch":
        if p.witch_shield_used:
            st["night1_shield_used"] = True
    elif r == "Jester":
        if p.jester_shield_used:
            st["night1_shield_used"] = True
    elif r == "Seer":
        hist_rows: List[List[int]] = []
        for x in p.seer_gazed_pairs:
            xs = sorted(int(i) for i in x)
            if len(xs) >= 2:
                hist_rows.append([xs[0], xs[1]])
        for pid in getattr(p, "seer_self_gaze_slots", set()) or set():
            hist_rows.append([int(pid), int(pid)])
        st["seer_pair_history"] = hist_rows

    if p.framed_tonight:
        st["is_framed"] = True
    if p.on_alert_tonight:
        st["is_on_alert"] = True
    if p.protected_tonight and r == "Survivor":
        st["is_vested"] = True
    elif getattr(p, "is_vested", False) and r == "Survivor":
        st["is_vested"] = True
    if p.ga_shield_active_tonight:
        st["ga_shield_active_tonight"] = True
    if p.tailored_as:
        st["is_tailored_as"] = p.tailored_as
    if p.guilty_next_day:
        st["guilty_tomorrow"] = True
    return st


def _role_state_to_player(pid: int, st: Dict[str, Any], p: Player, *, game_role: str) -> None:
    p.role = game_role
    if "vests_remaining" in st:
        p.vests_left = int(st.get("vests_remaining", 0))
    if "shots_remaining" in st:
        p.shots_left = int(st.get("shots_remaining", 0))
    p.guilty_next_day = bool(st.get("guilty_tomorrow", False))
    if "self_heals_remaining" in st:
        p.self_heals_left = int(st.get("self_heals_remaining", 0))
    if "uses_remaining" in st:
        u = int(st.get("uses_remaining", 0))
        if p.role == "Bodyguard":
            p.bg_uses_left = u
        elif p.role == "Gatekeeper":
            p.gatekeeper_uses_left = u
            last_tid = st.get("gatekeeper_last_guard_target_id")
            if last_tid is not None:
                try:
                    p.gatekeeper_last_guard_target_id = int(last_tid)
                except (TypeError, ValueError):
                    pass
            last_day = st.get("gatekeeper_last_successful_guard_day_number")
            if last_day is not None:
                try:
                    p.gatekeeper_last_successful_guard_day_number = int(last_day)
                except (TypeError, ValueError):
                    pass
        elif p.role == "Gravedigger":
            p.gravedigger_uses_left = u
        elif p.role == "Mole":
            p.mole_uses_left = u
        elif p.role == "Tailor":
            p.tailor_uses_left = u
        elif p.role == "Retributionist":
            p.retri_uses_left = u
        elif p.role == "Chaos":
            p.chaos_uses_left = u
    if "self_protects_remaining" in st:
        p.bg_self_protects_left = int(st.get("self_protects_remaining", 0))
    if "alerts_remaining" in st:
        p.alerts_left = int(st.get("alerts_remaining", 0))
    if "deputy_shots_remaining" in st:
        p.deputy_shots_remaining = int(st.get("deputy_shots_remaining", 0))
    p.sk_cautious = bool(st.get("sk_cautious", False))
    p.sk_suppressed_by_pirate = bool(st.get("sk_suppressed_by_pirate", False))
    if "wins" in st:
        p.pirate_wins = int(st.get("wins", 0))
    p.mayor_revealed = bool(st.get("is_revealed", False))
    p.ga_ward_charges = int(st.get("ga_ward_charges", p.ga_ward_charges))
    p.ga_defeated = bool(st.get("ga_defeated", False))
    bind = st.get("ga_target_id")
    p.ga_bind_id = int(bind) if bind is not None else p.ga_bind_id
    lock_day = st.get("ga_trial_lock_day")
    if lock_day is not None:
        try:
            p.ga_trial_lock_day = int(lock_day)
        except (TypeError, ValueError):
            pass
    exe = st.get("exe_target")
    p.exe_target = int(exe) if exe is not None else p.exe_target
    p.exe_won = bool(st.get("exe_won", False))
    p.witch_shield_used = bool(st.get("night1_shield_used", False)) if p.role == "Witch" else p.witch_shield_used
    p.chaos_shield_used = bool(st.get("night1_shield_used", False)) if p.role == "Chaos" else p.chaos_shield_used
    p.jester_shield_used = bool(st.get("night1_shield_used", False)) if p.role == "Jester" else p.jester_shield_used
    p.framed_tonight = bool(st.get("is_framed", False))
    p.on_alert_tonight = bool(st.get("is_on_alert", False))
    p.is_vested = bool(st.get("is_vested", False))
    p.protected_tonight = p.is_vested or bool(st.get("ga_shield_active_tonight", False))
    p.ga_shield_active_tonight = bool(st.get("ga_shield_active_tonight", False))
    tailored = st.get("is_tailored_as")
    p.tailored_as = str(tailored) if tailored else None
    hist = st.get("seer_pair_history") or []
    pairs: Set[frozenset[int]] = set()
    self_slots: Set[int] = set()
    for item in hist:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                a, b = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            if a == b:
                self_slots.add(a)
            else:
                pairs.add(frozenset({a, b}))
    if pairs or self_slots:
        p.seer_gazed_pairs = pairs
        p.seer_self_gaze_slots = self_slots


def expand_reanimate_actions(game: game_module.Game) -> None:
    from reanimate_expand import expand_reanimate_actions as _expand

    _expand(game)


def build_game_from_sim(
    players: List[Player],
    alive: Set[int],
    *,
    doused: Set[int],
    dead_town_corpses: List[Tuple[int, str]],
    used_corpse_ids: Set[int],
    hidden_corpse_ids: Set[int],
    day: int,
    guild_id: int = 424242,
) -> Tuple[game_module.Game, _FakeGuild]:
    members = [_FakeMember(p.i) for p in players if p.i in alive]
    g = game_module.Game(guild_id=guild_id)
    g.in_progress = True
    g.phase = "night"
    g.day_number = day
    g.players = members  # type: ignore[assignment]
    g.living_players = members.copy()  # type: ignore[assignment]
    g.player_roles = {p.i: p.role for p in players if p.i in alive}
    g.role_states = {p.i: _player_to_role_state(p) for p in players if p.i in alive}
    g.doused_players = set(int(x) for x in doused)
    g.graveyard = []
    for pid, role in dead_town_corpses:
        g.graveyard.append(
            {
                "player_id": pid,
                "real_role": role,
                "used_by_retri": pid in used_corpse_ids,
                "is_hidden": pid in hidden_corpse_ids,
            }
        )
    g.player_slots = {p.i: p.i for p in players}
    return g, _FakeGuild(members)


def actions_to_night_actions(actions: List[Action]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for act in actions:
        actor = act.get("actor")
        if actor is None:
            continue
        payload: Dict[str, Any] = dict(act)
        payload.pop("_from_chaos", None)
        a_type = payload.get("type")
        if a_type == "plunder" and "duel_finished" not in payload:
            payload["duel_finished"] = True
            payload["duel_outcome_ready"] = True
            if "duel_won" not in payload:
                payload["duel_won"] = False
        out[int(actor)] = payload
    return out


def _apparent_role(game: game_module.Game, tgt: int) -> str:
    real = game.player_roles.get(tgt, "Unknown")
    st = game.role_states.get(tgt, {}) or {}
    if st.get("is_framed"):
        return "Framer"
    if tgt in game.doused_players or real == "Arsonist":
        return "Arsonist"
    return real


def apply_evidence_from_night(
    game: game_module.Game,
    *,
    visit_log: Dict[int, List[int]],
    blocked: List[int],
    evidence: Dict[int, int],
) -> None:
    from engine.night import (
        effective_primary_target,
        effective_visit_house_for_submitted_target,
        track_followed_player_id,
    )
    from engine.night import _seer_bucket_for_player as engine_seer_bucket

    blocked_set = set(blocked)
    for actor_id, action in game.night_actions.items():
        if actor_id in blocked_set:
            continue
        a_type = action.get("type")
        if a_type == "investigate":
            tgt = effective_primary_target(game, actor_id)
            if tgt is None:
                continue
            role = action.get("role")
            apparent = _apparent_role(game, tgt)
            if role == "Sheriff":
                from scripts.monte_carlo import config as mc_config

                true_role = game.player_roles.get(tgt, "Unknown")
                framed = bool(game.role_states.get(tgt, {}).get("is_framed"))
                if mc_config.ARSONIST_SHERIFF_DETECTION_IMMUNE:
                    # True role only: Arsonist + douse overlays do not flag Sheriff-suspicious.
                    suspicious = true_role in ALL_MAFIA_ROLES or framed
                else:
                    suspicious = (
                        apparent in ALL_MAFIA_ROLES
                        or apparent == "Arsonist"
                        or framed
                    )
                if suspicious:
                    evidence[tgt] = evidence.get(tgt, 0) + 3
            elif role == "Investigator":
                true_role = game.player_roles.get(tgt, "Unknown")
                st = game.role_states.get(tgt, {}) or {}
                if (
                    true_role in ALL_MAFIA_ROLES
                    or true_role == "Arsonist"
                    or st.get("is_framed")
                    or tgt in game.doused_players
                ):
                    evidence[tgt] = evidence.get(tgt, 0) + 2
                else:
                    evidence[tgt] = max(0, evidence.get(tgt, 0) - 1)
            elif role == "Mole":
                if apparent in ALL_MAFIA_ROLES or apparent == "Arsonist":
                    evidence[tgt] = evidence.get(tgt, 0) + 3
        elif a_type == "watch":
            from engine.night import LOOKOUT_VISITOR_CAP, _lookout_visitors_excluding_self

            watched = effective_primary_target(game, actor_id)
            if watched is None:
                continue
            others = _lookout_visitors_excluding_self(visit_log.get(watched, []), actor_id)
            if len(others) > LOOKOUT_VISITOR_CAP:
                continue
            for v in others:
                if game.player_roles.get(v) in ALL_MAFIA_ROLES:
                    evidence[v] = evidence.get(v, 0) + 2
        elif a_type == "track":
            tracked = track_followed_player_id(action)
            if tracked is None:
                continue
            visited = [t for t, vs in visit_log.items() if tracked in vs]
            if visited and game.player_roles.get(tracked) in ALL_MAFIA_ROLES:
                evidence[tracked] = evidence.get(tracked, 0) + 2
        elif a_type == "gaze":
            if game.player_roles.get(actor_id) != "Seer":
                continue
            raw = action.get("targets")
            if not isinstance(raw, list) or len(raw) < 2:
                continue
            try:
                a_id, b_id = int(raw[0]), int(raw[1])
            except (TypeError, ValueError):
                continue
            a_id = effective_visit_house_for_submitted_target(game, a_id)
            b_id = effective_visit_house_for_submitted_target(game, b_id)
            ba = engine_seer_bucket(game, a_id)
            bb = engine_seer_bucket(game, b_id)
            if ba == 4 or bb == 4 or ba != bb:
                evidence[a_id] = evidence.get(a_id, 0) + 1
                evidence[b_id] = evidence.get(b_id, 0) + 1
        elif a_type == "hypnotize":
            try:
                tgt = int(action.get("target"))
            except (TypeError, ValueError):
                continue
            msg = action.get("msg_type", "")
            if msg in {"roleblocked", "controlled"}:
                evidence[tgt] = evidence.get(tgt, 0) + 1


async def _run_pipeline_async(
    game: game_module.Game, guild: _FakeGuild
) -> Tuple[Dict[int, List[int]], List[int], Dict[int, int], Dict[int, List[Dict[str, object]]], Set[int]]:
    return await run_night_pipeline(game, guild, deliver_feedback=False)  # type: ignore[arg-type]


def apply_psychic_evidence_for_sim(
    game: game_module.Game,
    blocked: List[int],
    evidence: Dict[int, int],
) -> None:
    """Lynch-AI evidence from Psychic visions (aligned with engine pool/threshold rules)."""
    from faction_taxonomy import (
        psychic_even_night_good_role,
        psychic_odd_evil_roles,
        psychic_vision_living_too_small,
        psychic_vision_pool_too_small_even,
        psychic_vision_pool_too_small_odd,
    )

    blocked_set = set(int(x) for x in blocked)
    living = {int(pid) for pid in game.player_roles}
    for psychic_id in [pid for pid, r in game.player_roles.items() if r == "Psychic"]:
        if psychic_id not in living or psychic_id in blocked_set:
            continue
        if psychic_vision_living_too_small(len(living)):
            continue
        pool = living - {psychic_id}
        day = int(getattr(game, "day_number", 0))
        if day % 2 == 1:
            evil_pool = [
                pid
                for pid in pool
                if (
                    game.player_roles.get(pid) in ALL_MAFIA_ROLES
                    or game.player_roles.get(pid) in psychic_odd_evil_roles()
                    or game.role_states.get(pid, {}).get("is_framed")
                    or pid in game.doused_players
                )
            ]
            if evil_pool and not psychic_vision_pool_too_small_odd(len(pool)):
                import random

                e = random.choice(evil_pool)
                evidence[e] = evidence.get(e, 0) + 2
        else:
            good_pool = [
                pid
                for pid in pool
                if psychic_even_night_good_role(game.player_roles.get(pid) or "")
            ]
            if good_pool and not psychic_vision_pool_too_small_even(len(pool)):
                import random

                g = random.choice(good_pool)
                evidence[g] = evidence.get(g, 0) + 1


def format_night_death_trace(game: game_module.Game) -> List[str]:
    lines: List[str] = []
    for pid in sorted(game.night_death_causes, key=lambda x: (str(game.night_death_causes[x]), x)):
        role = game.player_roles.get(pid, "?")
        cause = game.night_death_causes[pid]
        lines.append(f"Night death cause: P{pid} ({role}) -> {cause}")
    return lines


def resolve_night_via_engine(
    game: game_module.Game,
    guild: _FakeGuild,
    *,
    evidence: Dict[int, int],
) -> Tuple[Set[int], List[int], Dict[str, int], List[str]]:
    from night_resolve_prep import expand_reanimate_for_night_resolve

    expand_reanimate_for_night_resolve(game)
    visit_log, blocked, healed_by, protected_by, deaths = run_async(
        _run_pipeline_async(game, guild)
    )
    if mc_config.ENGINE_NIGHT_INVARIANTS:
        out = {
            "visit_log_raw": game._build_visit_log(),
            "visit_log": visit_log,
            "blocked": list(blocked),
            "healed_by_map": healed_by or {},
            "protected_by_map": protected_by or {},
            "deaths": set(int(x) for x in deaths),
            "night_transport_swaps": list(getattr(game, "night_transport_swaps", []) or []),
        }
        assert_post_night_pipeline_invariants(game, out)
        assert_night_outcome_consistency(game, out)
    from retributionist_consumption import consume_retributionist_uses

    consume_retributionist_uses(game, blocked, healed_by or {})
    guilt_deaths: Set[int] = set(int(x) for x in deaths)
    night_kills = set(guilt_deaths)
    from night_guilt import tally_guilt_and_jester_deaths

    _gv, _jh = run_async(
        tally_guilt_and_jester_deaths(game, guild, guilt_deaths, night_kills)  # type: ignore[arg-type]
    )
    deaths = guilt_deaths
    run_async(deliver_psychic_visions(game, guild, blocked))  # type: ignore[arg-type]
    apply_psychic_evidence_for_sim(game, blocked, evidence)
    apply_evidence_from_night(game, visit_log=visit_log, blocked=blocked, evidence=evidence)
    stat_deltas = {
        "roleblocks": sum(
            1
            for pid in blocked
            if pid in game.player_roles and game.night_actions.get(pid, {}).get("blocked_by_roleblock")
        ),
        "gatekeeper_blocks": sum(
            1
            for pid in blocked
            if game.night_actions.get(pid, {}).get("blocked_by_gatekeeper")
        ),
        "ignites": sum(1 for _pid, cause in game.night_death_causes.items() if cause == "arsonist_ignite"),
        "doc_saves": sum(
            1 for tgt, healer in (healed_by or {}).items() if game.player_roles.get(healer) == "Doctor"
        ),
    }
    return set(int(x) for x in guilt_deaths), list(blocked), stat_deltas, format_night_death_trace(game)


async def deputy_day_shot(
    game: game_module.Game,
    guild: _FakeGuild,
    deputy_id: int,
    target_id: int,
) -> Set[int]:
    """Mirror bot_app/night_cmds.py Deputy daytime revolver + process_death."""
    deaths: Set[int] = set()
    st = game.role_states.setdefault(deputy_id, {})
    if int(st.get("deputy_shots_remaining", 0)) <= 0:
        return deaths
    if game.deputy_fired_today(deputy_id):
        return deaths

    evil = _deputy_gun_sees_evil(game, target_id)
    armored = _deputy_target_basic_defense(game, target_id)
    game.mark_deputy_shot_today(deputy_id)

    ctx = _FakeChanCtx()
    if evil and armored:
        return deaths
    if evil and not armored:
        await game.process_death_by_id(ctx, guild, target_id, "deputy_shoot")  # type: ignore[arg-type]
        deaths.add(target_id)
        return deaths

    await game.process_death_by_id(ctx, guild, target_id, "deputy_friendly_fire")  # type: ignore[arg-type]
    deaths.add(target_id)
    await game.process_death_by_id(ctx, guild, deputy_id, "deputy_friendly_fire_self")  # type: ignore[arg-type]
    deaths.add(deputy_id)
    return deaths


async def resolve_jester_haunts(
    game: game_module.Game,
    guild: _FakeGuild,
    pending_haunts: List[object],
) -> Set[int]:
    """
    Mirror gm.py post-pipeline Jester haunt (eligible voters: guilty + abstain, cause=haunt).
    pending_haunts: objects with jester_id, guilty_voters attrs (eligible haunt pool).
    """
    from scripts.monte_carlo.day import PendingJesterHaunt, pick_haunt_target
    from scripts.monte_carlo.state import Player as SimPlayer

    deaths: Set[int] = set()
    await game.sync_living_players(guild)  # type: ignore[arg-type]
    living_ids = set(await game.get_living_ids(guild))  # type: ignore[arg-type]
    ctx = _FakeChanCtx()
    sim_players = [
        SimPlayer(i=int(pid), role=str(game.player_roles.get(int(pid), "?")), alive=int(pid) in living_ids)
        for pid in game.player_roles
    ]

    for entry in pending_haunts:
        jester_id = int(getattr(entry, "jester_id", 0))
        guilty_voters: List[int] = list(getattr(entry, "guilty_voters", []))
        st = game.role_states.setdefault(jester_id, {})
        st["can_haunt"] = True
        st["jester_won"] = True
        st["guilty_voters"] = guilty_voters
        if "haunt_target" in st:
            continue
        pending = PendingJesterHaunt(jester_id=jester_id, guilty_voters=guilty_voters)
        target = pick_haunt_target(sim_players, living_ids, pending)
        if target is None:
            continue
        st["haunt_target"] = int(target)
        target = int(st["haunt_target"])
        await game.process_death_by_id(ctx, guild, target, "haunt")  # type: ignore[arg-type]
        deaths.add(target)
        st.pop("haunt_target", None)
        st["can_haunt"] = False

    for _pid, st in list(game.role_states.items()):
        st.pop("haunt_target", None)
    return deaths


def sync_engine_to_sim(
    game: game_module.Game,
    players: List[Player],
    alive: Set[int],
    *,
    doused: Set[int],
    used_corpse_ids: Set[int],
) -> None:
    doused.clear()
    doused.update(int(x) for x in game.doused_players)
    used_corpse_ids.clear()
    for entry in game.graveyard:
        if entry.get("used_by_retri"):
            try:
                used_corpse_ids.add(int(entry["player_id"]))
            except (TypeError, ValueError):
                continue
    for pid, role in game.player_roles.items():
        if role != "Retributionist":
            continue
        raw = (game.role_states.get(int(pid), {}) or {}).get("used_corpses", [])
        if not isinstance(raw, list):
            continue
        for x in raw:
            try:
                used_corpse_ids.add(int(x))
            except (TypeError, ValueError):
                continue
    for p in players:
        if p.i not in alive:
            continue
        st = game.role_states.get(p.i, {})
        role = game.player_roles.get(p.i, p.role)
        _role_state_to_player(p.i, st, p, game_role=role)
        p.doused = p.i in doused


async def sweep_executioner_conversions(
    game: game_module.Game, guild: _FakeGuild, deaths: Set[int]
) -> None:
    for pid in deaths:
        await game._sweep_executioner_conversion_for(  # type: ignore[attr-defined]
            guild, dead_player_id=int(pid), cause="night"
        )


def sweep_executioner_conversions_from_sim(
    players: List[Player],
    alive: Set[int],
    death_ids: Set[int],
    *,
    doused: Set[int],
    dead_town_corpses: List[Tuple[int, str]],
    used_corpse_ids: Set[int],
    hidden_corpse_ids: Set[int],
    day: int,
) -> None:
    """Apply EXE→Jester (or exe_won on lynch) when deaths occur outside the night pipeline."""
    if not death_ids:
        return
    game, guild = build_game_from_sim(
        players,
        alive,
        doused=doused,
        dead_town_corpses=dead_town_corpses,
        used_corpse_ids=used_corpse_ids,
        hidden_corpse_ids=hidden_corpse_ids,
        day=day,
    )
    run_async(sweep_executioner_conversions(game, guild, death_ids))
    sync_engine_to_sim(game, players, alive, doused=doused, used_corpse_ids=used_corpse_ids)

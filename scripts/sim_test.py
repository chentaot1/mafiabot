from __future__ import annotations

import asyncio
import argparse
import hashlib
import itertools
import json
import math
import copy
import random
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Callable

import discord

# Allow running as `python scripts/sim_test.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import Game
import game_roles
from scripts.monte_carlo import generator as mc_gen
from scripts.monte_carlo.runtime import default_trial_workers
import config as bot_config
from invariants import (
    assert_game_runtime_sanity as _assert_game_runtime_sanity,
    assert_post_night_pipeline_invariants as _assert_post_night_pipeline_invariants,
)

REPRO_DIR = ROOT / "tests" / "repros"


@dataclass
class FakeMember:
    id: int
    display_name: str
    roles: List[object] = field(default_factory=list)
    inbox: List[str] = field(default_factory=list)
    guild: Optional["FakeGuild"] = None
    voice: None = None

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def send(self, content: str) -> None:
        # Collect DMs for debugging; never fail.
        self.inbox.append(str(content))

    async def add_roles(self, *_roles: object) -> None:
        # Used by sync_living_players when alive_role_id is set; no-op here.
        return

    async def remove_roles(self, *_roles: object) -> None:
        return

    async def move_to(self, *_args: object, **_kwargs: object) -> None:
        return


@dataclass
class FakeChannel:
    messages: List[str] = field(default_factory=list)
    guild: Optional["FakeGuild"] = None

    async def send(self, content: str, **kwargs) -> None:
        self.messages.append(str(content))


class FakeGuild:
    def __init__(self, guild_id: int, members: Dict[int, FakeMember]) -> None:
        self.id = guild_id
        self._members = members

    def get_member(self, user_id: int) -> Optional[FakeMember]:
        return self._members.get(int(user_id))

    async def fetch_member(self, user_id: int) -> FakeMember:
        m = self.get_member(user_id)
        if m is None:
            # Match discord.py-ish behavior expected by get_member_safe.
            class _DummyResp:
                status = 404
                reason = "Not Found"

            raise discord.NotFound(response=_DummyResp(), message="Member not found")  # type: ignore[arg-type]
        return m

    def get_role(self, _role_id: int) -> None:
        return None

    def get_channel(self, _channel_id: Optional[int]) -> None:
        return None


async def run_night_pipeline(game: Game, guild: FakeGuild) -> Dict[str, Any]:
    """
    Executes the same engine pipeline used by `!resolve`, but without any channel I/O.
    Returns a small dict of intermediate artifacts for assertions/debugging.
    """
    from engine.night import run_night_pipeline as engine_run_night_pipeline

    from reanimate_expand import expand_reanimate_actions

    expand_reanimate_actions(game, strict=True)
    visit_log, blocked, healed_by_map, protected_by_map, deaths = await engine_run_night_pipeline(
        game, guild  # type: ignore[arg-type]
    )
    blocked_list = list(blocked)
    blocked_set = set(blocked_list)
    visit_log_raw = game._build_visit_log()
    return {
        "visit_log_raw": visit_log_raw,
        "visit_log": visit_log,
        "blocked": blocked_list,
        "healed_by_map": healed_by_map,
        "protected_by_map": protected_by_map,
        "deaths": set(deaths),
        "night_transport_swaps": list(getattr(game, "night_transport_swaps", []) or []),
    }


async def _sim_noop_persist_flush(self: Game) -> None:
    """Sim runs must not write ``state/{guild_id}.json`` (parallel + Windows file locks)."""
    return


def make_game(*, seed: int = 1, n: int = 8) -> tuple[Game, FakeGuild, Dict[int, FakeMember]]:
    random.seed(seed)
    # Unique guild per seed so parallel systematic workers do not collide on disk.
    guild_id = 10_000 + (int(seed) % 890_000)

    members: Dict[int, FakeMember] = {i: FakeMember(i, f"P{i}") for i in range(1, n + 1)}
    guild = FakeGuild(guild_id, members)
    for m in members.values():
        m.guild = guild

    game = Game(guild_id)
    game.game_key = f"sim:{guild_id}"
    game.persist_flush = _sim_noop_persist_flush.__get__(game, Game)  # type: ignore[method-assign]
    game.in_progress = True
    game.phase = "night"
    # Keep role drift repair logic disabled for sims.
    game.alive_role_id = None

    # "Players" and "living_players" are discord.Member-like objects; we provide FakeMember.
    game.players = list(members.values())  # type: ignore[assignment]
    game.living_players = list(members.values())  # type: ignore[assignment]

    # Ensure dicts exist.
    game.player_roles = {}
    game.role_states = {}
    game.night_actions = {}
    game.graveyard = []

    return game, guild, members


from per_night_state import all_keys_cleared_at_start_night

# Ephemeral per-night flags cleared by Game.start_night (mirrored for sim resets).
_NIGHT_EPHEMERAL_ROLE_STATE_KEYS: Tuple[str, ...] = all_keys_cleared_at_start_night()

_VARIANT_LIST_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def reset_night_game_state(
    game: Game,
    *,
    members: Dict[int, FakeMember],
    roles_by_seat: Dict[int, str],
    day_number: int = 1,
    clear_member_inboxes: bool = False,
) -> None:
    """
    Restore a fresh night-1 sandbox on an existing Game (all seats alive, counters re-init).
    Use between sim nights so deaths/douses/visits do not bleed across scenarios.
    """
    game.in_progress = True
    game.phase = "night"
    game.day_number = int(day_number)
    game.players = list(members.values())  # type: ignore[assignment]
    game.living_players = list(members.values())  # type: ignore[assignment]
    game.player_roles = dict(roles_by_seat)
    game.night_actions = {}
    game.graveyard = []
    game.doused_players.clear()
    game.night_death_causes.clear()
    game.night_completion_snapshot = None
    game.psychic_visions_delivered_this_night = False
    game.night_transport_swaps = []
    game._transport_pairs_seen = set()
    game._effective_visit_destinations_cache = None

    for seat, role in roles_by_seat.items():
        st = dict(_init_state_for_role(role))
        for key in _NIGHT_EPHEMERAL_ROLE_STATE_KEYS:
            st.pop(key, None)
        game.role_states[int(seat)] = st

    if clear_member_inboxes:
        for m in members.values():
            m.inbox.clear()


def _variants_for_role(role: str) -> List[Dict[str, Any]]:
    cached = _VARIANT_LIST_CACHE.get(role)
    if cached is not None:
        return cached
    variants = (
        [{"type": "noop"}]
        + _systematic_variants_for_role(role)
        + _corruption_variants_for_role(role)
    )
    _VARIANT_LIST_CACHE[role] = variants
    return variants


def _canonical_night_actions_key(actions: Dict[int, Dict[str, Any]]) -> str:
    def _norm_obj(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {str(k): _norm_obj(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
        if isinstance(obj, (list, tuple)):
            return [_norm_obj(x) for x in obj]
        return obj

    payload = {str(k): _norm_obj(v) for k, v in sorted(actions.items(), key=lambda kv: int(kv[0]))}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _materialize_combo_seed(
    base_seed: int,
    roleset_index: int,
    seat_combo: Tuple[int, ...],
    variants: Tuple[Dict[str, Any], ...],
) -> int:
    blob = json.dumps([list(seat_combo), *variants], sort_keys=True, default=str)
    digest = int(hashlib.md5(blob.encode("utf-8")).hexdigest()[:8], 16)
    seat_mix = 0
    for sid in seat_combo:
        seat_mix = seat_mix * 1_009 + int(sid)
    return (
        int(base_seed)
        + int(roleset_index) * 1_000_003
        + seat_mix * 10_007
        + digest
    ) & 0x7FFFFFFF


@dataclass
class SystematicRunStats:
    nights_executed: int = 0
    nights_deduped: int = 0


def assert_invariants(game: Game) -> None:
    _assert_game_runtime_sanity(game)


def assert_post_night_invariants(game: Game, out: Dict[str, Any]) -> None:
    _assert_post_night_pipeline_invariants(game, out)


def _write_repro(*, name: str, payload: Dict[str, Any]) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPRO_DIR / f"{ts}_{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def scenario_transport_redirects_target() -> None:
    game, guild, members = make_game(seed=10, n=6)
    # Roles: Transporter swaps 2<->3; Vigilante shoots 2, should hit 3.
    game.player_roles.update({1: "Transporter", 4: "Vigilante", 2: "Townie", 3: "Townie", 5: "Townie", 6: "Townie"})
    game.role_states[4] = {"shots_remaining": 1}

    game.night_actions[1] = {"type": "transport", "actor": 1, "targets": [2, 3]}
    game.night_actions[4] = {"type": "shoot", "actor": 4, "target": 2}

    out = await run_night_pipeline(game, guild)
    assert 3 in out["deaths"], f"Expected transported target (3) to die, got deaths={out['deaths']}"
    assert_post_night_invariants(game, out)


async def scenario_control_immune_role_not_redirected() -> None:
    game, guild, _members = make_game(seed=20, n=6)
    # Witch attempts to control Chaos (immune). Chaos has no action here; we just assert no crash and no forced action inserted.
    game.player_roles.update({1: "Witch", 2: "Chaos", 3: "Townie", 4: "Townie", 5: "Townie", 6: "Townie"})
    game.role_states[2] = {"uses_remaining": 1}
    game.night_actions[1] = {"type": "control", "actor": 1, "targets": [2, 3]}

    out = await run_night_pipeline(game, guild)
    # Contract: Chaos is control-immune, so Witch should not inject/redirect an action for Chaos.
    assert game.night_actions.get(2) is None, "Chaos should have no action injected by Witch control"
    assert game.night_actions.get(1, {}).get("type") == "control", "Witch action should remain a control"
    assert_post_night_invariants(game, out)


async def scenario_corrupted_actions_do_not_crash() -> None:
    game, guild, _members = make_game(seed=30, n=6)
    game.player_roles.update({1: "Transporter", 2: "Witch", 3: "Townie", 4: "Townie", 5: "Townie", 6: "Townie"})

    # Malformed action payloads: wrong shapes/types.
    game.night_actions[1] = {"type": "transport", "actor": 1, "role": "Transporter", "targets": ["oops", None]}
    game.night_actions[2] = {"type": "control", "actor": 2, "role": "Witch", "targets": ["x", "y"]}
    # Engine assumes `role` exists because set_night_action() injects it; fuzz target type instead.
    game.night_actions[3] = {"type": "investigate", "actor": 3, "role": "Investigator", "target": "not-an-int"}

    out = await run_night_pipeline(game, guild)
    assert_post_night_invariants(game, out)


async def scenario_gatekeeper_corrupted_guard_target_does_not_crash() -> None:
    game, guild, _members = make_game(seed=31, n=5)
    game.player_roles.update({1: "Gatekeeper", 2: "Doctor", 3: "Mobster", 4: "Townie", 5: "Townie"})
    game.role_states[1] = {"uses_remaining": 1}
    game.role_states[2] = {"self_heals_remaining": 1}

    # Corrupted persisted target: unhashable target should be ignored, not crash resolve_blocking().
    game.night_actions[1] = {"type": "guard", "actor": 1, "role": "Gatekeeper", "target": []}
    # Include some other actions to ensure pipeline still runs.
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 4}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}

    out = await run_night_pipeline(game, guild)
    assert_post_night_invariants(game, out)


async def scenario_graveyard_corruption_does_not_crash_sync() -> None:
    game, guild, members = make_game(seed=40, n=4)
    game.player_roles.update({1: "Townie", 2: "Townie", 3: "Townie", 4: "Townie"})

    # Simulate corrupted persisted data.
    game.graveyard = [{"player_id": "NaN", "real_role": "Townie"}, {"player_id": 2, "real_role": "Townie"}]
    # Also ensure living list contains player 2; sync should remove them but not crash.
    game.living_players = [members[1], members[2], members[3], members[4]]  # type: ignore[assignment]

    await game.sync_living_players(guild)  # type: ignore[arg-type]
    assert all(p.id != 2 for p in game.living_players), "Expected player_id=2 graveyard entry to remove from living_players"


async def scenario_ignite_is_unstoppable_even_through_heal() -> None:
    game, guild, _members = make_game(seed=70, n=5)
    # Seat 1 is Arsonist; seat 3 is Doctor healing seat 2.
    game.player_roles.update({1: "Arsonist", 2: "Townie", 3: "Doctor", 4: "Townie", 5: "Townie"})
    game.role_states[3] = {"self_heals_remaining": 1}

    # Target 2 is doused prior to this night.
    game.doused_players = {2}
    game.night_actions[1] = {"type": "ignite", "actor": 1, "role": "Arsonist"}
    game.night_actions[3] = {"type": "heal", "actor": 3, "role": "Doctor", "target": 2}

    out = await run_night_pipeline(game, guild)
    assert 2 in out["deaths"], "Ignite deaths should be unstoppable (heal should not prevent)"
    assert_post_night_invariants(game, out)


async def scenario_transport_redirects_witch_control_targets() -> None:
    from engine.night import effective_primary_target

    game, guild, _members = make_game(seed=80, n=7)
    game.player_roles.update(
        {
            1: "Transporter",
            2: "Witch",
            3: "Vigilante",
            4: "Townie",
            5: "Townie",
            6: "Townie",
            7: "Townie",
        }
    )
    game.role_states[3] = {"shots_remaining": 1}

    # Transport swaps 5 <-> 6; Witch forces Vigilante to shoot 5 (effective visit 6).
    game.night_actions[1] = {"type": "transport", "actor": 1, "role": "Transporter", "targets": [5, 6]}
    game.night_actions[2] = {"type": "control", "actor": 2, "role": "Witch", "targets": [3, 5]}
    game.night_actions[3] = {"type": "shoot", "actor": 3, "role": "Vigilante", "target": 4}

    out = await run_night_pipeline(game, guild)
    assert int(game.night_actions[3]["target"]) == 5
    assert effective_primary_target(game, 3) == 6
    assert 6 in out["deaths"], f"Vigilante shot should land on transported slot 6; deaths={out['deaths']}"
    assert_post_night_invariants(game, out)


async def scenario_transport_does_not_redirect_self_only_actions() -> None:
    from engine.night import effective_primary_target

    game, guild, _members = make_game(seed=81, n=6)
    game.player_roles.update(
        {
            1: "Transporter",
            2: "Doctor",
            3: "Survivor",
            4: "Mobster",
            5: "Townie",
            6: "Townie",
        }
    )
    game.role_states[2] = {"self_heals_remaining": 1}
    game.night_actions[1] = {"type": "transport", "actor": 1, "role": "Transporter", "targets": [5, 6]}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 5}
    game.night_actions[3] = {"type": "vest", "actor": 3, "role": "Survivor"}

    out = await run_night_pipeline(game, guild)
    assert int(game.night_actions[2]["target"]) == 5
    assert effective_primary_target(game, 2) == 6
    assert out["healed_by_map"].get(6) == 2
    assert game.night_actions[3].get("type") == "vest"
    assert_post_night_invariants(game, out)


async def scenario_witch_cannot_retarget_self_only_actions() -> None:
    game, guild, _members = make_game(seed=82, n=5)
    game.player_roles.update({1: "Witch", 2: "Survivor", 3: "Townie", 4: "Townie", 5: "Townie"})
    game.role_states[2] = {"vests_remaining": 2}
    game.night_actions[2] = {"type": "vest", "actor": 2, "role": "Survivor"}
    game.night_actions[1] = {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 3]}

    out = await run_night_pipeline(game, guild)
    assert game.night_actions[2].get("type") == "vest", "Witch should not redirect self-only actions"
    assert_post_night_invariants(game, out)


async def scenario_witch_can_prevent_arsonist_ignite_by_forcing_douse() -> None:
    game, guild, _members = make_game(seed=83, n=5)
    game.player_roles.update({1: "Witch", 2: "Arsonist", 3: "Townie", 4: "Townie", 5: "Townie"})
    # Arsonist intends to ignite; Witch controls Arsonist to target 4, which should convert ignite -> douse(4).
    game.night_actions[2] = {"type": "ignite", "actor": 2, "role": "Arsonist"}
    game.night_actions[1] = {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 4]}

    out = await run_night_pipeline(game, guild)
    act2 = game.night_actions[2]
    assert act2.get("type") == "douse", "Witch control should convert Arsonist ignite into douse"
    assert act2.get("target") == 4, "Witch-forced douse should target Witch's chosen victim"
    assert_post_night_invariants(game, out)


async def scenario_arsonist_ignite_while_doused_kills_self() -> None:
    game, guild, _members = make_game(seed=84, n=4)
    game.player_roles.update({1: "Arsonist", 2: "Townie", 3: "Townie", 4: "Doctor"})
    # Design intent: Ignite is Unstoppable in this ruleset (see config IGNITE_ATTACK_TIER).
    # It bypasses defense and also kills the Arsonist if they are currently doused.
    game.doused_players = {1, 2}
    game.night_actions[1] = {"type": "ignite", "actor": 1, "role": "Arsonist"}
    out = await run_night_pipeline(game, guild)
    assert 1 in out["deaths"], "Igniting while doused should kill the Arsonist too"
    assert 2 in out["deaths"], "Ignite should kill all doused victims"
    assert_post_night_invariants(game, out)


async def scenario_arsonist_basic_defense_survives_normal_kill() -> None:
    game, guild, _members = make_game(seed=85, n=3)
    game.player_roles.update({1: "Arsonist", 2: "Mobster", 3: "Doctor"})
    game.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 1 not in out["deaths"], "Arsonist should have basic defense against normal kills"
    assert game.role_states.get(1, {}).get("attacked_tonight_reason") == "survived", "Expected attacked_tonight_reason='survived'"
    assert_post_night_invariants(game, out)


async def scenario_transport_does_not_redirect_pirate_plunder() -> None:
    game, guild, _members = make_game(seed=86, n=6)
    game.player_roles.update(
        {
            1: "Transporter",
            2: "Pirate",
            3: "Townie",
            4: "Townie",
            5: "Townie",
            6: "Townie",
        }
    )
    # Transport swaps 5 <-> 6.
    game.night_actions[1] = {"type": "transport", "actor": 1, "role": "Transporter", "targets": [5, 6]}
    # Pirate plunders 5; should not be redirected to 6.
    game.night_actions[2] = {"type": "plunder", "actor": 2, "role": "Pirate", "target": 5, "duel_won": True, "duel_finished": True}
    from engine.night import effective_primary_target

    out = await run_night_pipeline(game, guild)
    assert int(game.night_actions[2]["target"]) == 5
    assert effective_primary_target(game, 2) == 5, "Transport must not redirect Pirate plunder visit"
    assert_post_night_invariants(game, out)


async def scenario_witch_redirects_mafia_kill_target() -> None:
    game, guild, _members = make_game(seed=87, n=5)
    game.player_roles.update({1: "Witch", 2: "Mobster", 3: "Townie", 4: "Townie", 5: "Townie"})
    game.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 3}
    # Witch controls Mobster to target 5 instead.
    game.night_actions[1] = {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 5]}
    from engine.night import effective_primary_target

    out = await run_night_pipeline(game, guild)
    assert int(game.night_actions[2]["target"]) == 5, "Witch should redirect Mobster kill target in action row"
    assert effective_primary_target(game, 2) == 5
    assert 5 in out["deaths"]
    assert_post_night_invariants(game, out)


async def scenario_executioner_converts_to_jester_on_target_non_lynch() -> None:
    game, guild, members = make_game(seed=190, n=4)
    ch = FakeChannel()
    ch.guild = guild
    # Executioner target is 2. If 2 dies to night kill, EXE becomes Jester.
    game.player_roles.update({1: "Executioner", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"exe_target": 2}
    # Ensure player 2 is considered living.
    game.living_players = [members[1], members[2], members[3], members[4]]  # type: ignore[assignment]

    await game.process_death(ch, members[2], cause="night")
    assert game.player_roles.get(1) == "Jester", "Executioner should convert to Jester if target dies non-lynch"


async def scenario_executioner_marks_win_on_lynch() -> None:
    game, guild, members = make_game(seed=191, n=4)
    ch = FakeChannel()
    ch.guild = guild
    game.player_roles.update({1: "Executioner", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"exe_target": 2}
    game.living_players = [members[1], members[2], members[3], members[4]]  # type: ignore[assignment]

    await game.process_death(ch, members[2], cause="lynch", voters=[members[3]])
    assert game.role_states.get(1, {}).get("exe_won") is True, "Executioner should mark exe_won on lynch of target"

async def scenario_arsonist_clean_removes_douse() -> None:
    game, guild, _members = make_game(seed=90, n=4)
    game.player_roles.update({1: "Arsonist", 2: "Townie", 3: "Townie", 4: "Townie"})
    # Simulate being doused earlier.
    game.doused_players = {1}
    game.night_actions[1] = {"type": "clean", "actor": 1, "role": "Arsonist"}

    out = await run_night_pipeline(game, guild)
    assert 1 not in game.doused_players, "Arsonist clean should remove self from doused_players"
    assert_post_night_invariants(game, out)


async def scenario_bodyguard_blocked_does_not_protect() -> None:
    game, guild, _members = make_game(seed=100, n=6)
    # BG tries to protect 4, but is roleblocked by Escort. Mafia attacks 4; 4 should die.
    game.player_roles.update({1: "Bodyguard", 2: "Escort", 3: "Mobster", 4: "Doctor", 5: "Townie", 6: "Townie"})
    game.role_states[1] = {"uses_remaining": 1, "self_protects_remaining": 1}
    game.role_states[2] = {}
    game.night_actions[1] = {"type": "protect", "actor": 1, "role": "Bodyguard", "target": 4}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert 4 in out["deaths"], "Roleblocked Bodyguard should not protect their target"
    assert_post_night_invariants(game, out)


async def scenario_vigilante_guilt_town_vs_mafia() -> None:
    # Town shot triggers guilt only if the Town member actually dies.
    game, guild, _members = make_game(seed=110, n=4)
    game.player_roles.update({1: "Vigilante", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"shots_remaining": 1}
    game.night_actions[1] = {"type": "shoot", "actor": 1, "role": "Vigilante", "target": 2}
    out = await run_night_pipeline(game, guild)
    assert 2 in out["deaths"], "Setup sanity: Town target should die in this scenario"
    assert game.role_states.get(1, {}).get("guilty_tomorrow") is True, "Vigilante should gain guilt when killing Town"
    assert_post_night_invariants(game, out)

    # Town survives (healed) -> no guilt.
    game_survive, guild_survive, _members_survive = make_game(seed=112, n=4)
    game_survive.player_roles.update({1: "Vigilante", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game_survive.role_states[1] = {"shots_remaining": 1}
    game_survive.night_actions[1] = {"type": "shoot", "actor": 1, "role": "Vigilante", "target": 2}
    game_survive.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 2}
    out_survive = await run_night_pipeline(game_survive, guild_survive)
    assert 2 not in out_survive["deaths"], "Setup sanity: Town target should survive when healed"
    assert game_survive.role_states.get(1, {}).get("guilty_tomorrow") is not True, "Vigilante should not gain guilt if Town survives"
    assert_post_night_invariants(game_survive, out_survive)

    # Mafia shot should not trigger guilt.
    game2, guild2, _members2 = make_game(seed=111, n=3)
    game2.player_roles.update({1: "Vigilante", 2: "Mobster", 3: "Doctor"})
    game2.role_states[1] = {"shots_remaining": 1}
    game2.night_actions[1] = {"type": "shoot", "actor": 1, "role": "Vigilante", "target": 2}
    out2 = await run_night_pipeline(game2, guild2)
    assert game2.role_states.get(1, {}).get("guilty_tomorrow") is not True, "Vigilante should not gain guilt when shooting Mafia"
    assert_post_night_invariants(game2, out2)


async def scenario_blocked_investigator_gets_interrupt() -> None:
    game, guild, members = make_game(seed=122, n=4)
    game.player_roles.update({1: "Investigator", 2: "Escort", 3: "Doctor", 4: "Mobster"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Investigator", "target": 3}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "could not investigate")
    assert_post_night_invariants(game, out)


async def scenario_blocked_tracker_gets_track_interrupt() -> None:
    game, guild, members = make_game(seed=124, n=4)
    game.player_roles.update({1: "Tracker", 2: "Escort", 3: "Doctor", 4: "Mobster"})
    game.night_actions[1] = {"type": "track", "actor": 1, "role": "Tracker", "target": 4}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "could not track")
    assert_post_night_invariants(game, out)


async def scenario_blocked_lookout_gets_watch_interrupt() -> None:
    game, guild, members = make_game(seed=125, n=4)
    game.player_roles.update({1: "Lookout", 2: "Escort", 3: "Doctor", 4: "Mobster"})
    game.night_actions[1] = {"type": "watch", "actor": 1, "role": "Lookout", "target": 3}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "could not watch")
    assert_post_night_invariants(game, out)


async def scenario_witch_receives_controlled_sheriff_result() -> None:
    game, guild, members = make_game(seed=126, n=3)
    game.day_number = 2
    game.player_roles.update({1: "Witch", 2: "Sheriff", 3: "Mobster"})
    game.role_states[1] = {"has_learned_role": False, "night1_shield_used": False}
    game.night_actions = {
        1: {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "role": "Sheriff", "target": 1},
    }
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[2], "suspicious")
    assert_dm_received(members[1], "suspicious")
    assert_post_night_invariants(game, out)


async def scenario_witch_receives_controlled_investigator_bucket() -> None:
    game, guild, members = make_game(seed=127, n=3)
    game.day_number = 2
    game.player_roles.update({1: "Witch", 2: "Investigator", 3: "Mobster"})
    game.role_states[1] = {"has_learned_role": False, "night1_shield_used": False}
    game.night_actions = {
        1: {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "role": "Investigator", "target": 1},
    }
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[2], "could be")
    assert_dm_received(members[1], "could be")
    assert_post_night_invariants(game, out)


async def scenario_mobster_investigator_protective_shield_bucket() -> None:
    game, guild, members = make_game(seed=128, n=3)
    game.player_roles.update({1: "Investigator", 2: "Mobster", 3: "Doctor"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Investigator", "target": 2}
    out = await run_night_pipeline(game, guild)
    txt = "\n".join(members[1].inbox)
    assert "Bodyguard" in txt and "Mobster" in txt, txt
    assert "Vigilante" not in txt, "Mobster must resolve to bucket 9 only, not Loaded Guns"
    assert_post_night_invariants(game, out)


async def scenario_chaos_records_visit_targets() -> None:
    game, guild, _members = make_game(seed=129, n=4)
    game.player_roles.update({1: "Chaos", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"uses_remaining": 2}
    game.night_actions[1] = {"type": "chaos", "actor": 1, "role": "Chaos", "targets": [2, 3]}
    out = await run_night_pipeline(game, guild)
    st = game.role_states.get(1, {})
    targets = st.get("chaos_visit_targets")
    assert st.get("chaos_used_this_night") is True
    assert isinstance(targets, list) and set(int(x) for x in targets) == {2, 3}
    assert_post_night_invariants(game, out)


async def scenario_corrupt_misc_snapshot_still_heals() -> None:
    """misc_phase_complete without healed_by must not skip Doctor heals (resume guard)."""
    game, guild, _members = make_game(seed=131, n=4)
    game.player_roles.update({1: "Doctor", 2: "Mobster", 3: "Townie", 4: "Townie"})
    game.role_states[1] = {"self_heals_remaining": 1}
    game.night_completion_snapshot = {
        "day": game.day_number,
        "game_key": game.game_key,
        "misc_phase_complete": True,
    }
    game.night_actions[1] = {"type": "heal", "actor": 1, "role": "Doctor", "target": 3}
    game.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert 3 not in out["deaths"], "Heal must apply when heal map was not snapshotted"
    assert_post_night_invariants(game, out)


async def scenario_investigative_checkpoint_no_duplicate_watch_dm() -> None:
    """Second pipeline pass with investigative_phase_complete must not re-DM Lookout."""
    game, guild, members = make_game(seed=132, n=3)
    game.player_roles.update({1: "Lookout", 2: "Doctor", 3: "Mobster"})
    game.night_actions[1] = {"type": "watch", "actor": 1, "role": "Lookout", "target": 2}
    await run_night_pipeline(game, guild)
    assert len(members[1].inbox) == 1
    await run_night_pipeline(game, guild)
    assert len(members[1].inbox) == 1, "Checkpoint should suppress duplicate investigative DM"


async def scenario_lookout_excludes_self_heal_from_visitors() -> None:
    game, guild, members = make_game(seed=133, n=3)
    game.player_roles.update({1: "Lookout", 2: "Doctor", 3: "Mobster"})
    game.night_actions[1] = {"type": "watch", "actor": 1, "role": "Lookout", "target": 2}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 2}
    out = await run_night_pipeline(game, guild)
    txt = "\n".join(members[1].inbox).lower()
    assert "p1" not in txt, "Lookout must not list their own seat as a visitor"
    assert_post_night_invariants(game, out)


async def scenario_witch_steals_psychic_vision() -> None:
    game, guild, members = make_game(seed=134, n=5)
    game.day_number = 3
    game.player_roles.update(
        {1: "Witch", 2: "Psychic", 3: "Mobster", 4: "Doctor", 5: "Townie"}
    )
    game.role_states[1] = {"has_learned_role": False, "night1_shield_used": False}
    game.night_actions[1] = {"type": "control", "actor": 1, "role": "Witch", "targets": [2, 3]}
    out = await run_night_pipeline(game, guild)
    witch_txt = "\n".join(members[1].inbox).lower()
    assert "bent" in witch_txt or "stolen vision" in witch_txt, members[1].inbox
    assert_post_night_invariants(game, out)


async def scenario_transport_ack_uses_effective_visit_house() -> None:
    from engine.night import effective_primary_target

    game, _guild, _members = make_game(seed=135, n=4)
    game.player_roles.update({1: "Transporter", 2: "Sheriff", 3: "Mobster", 4: "Doctor"})
    game.night_transport_swaps = [(2, 3, 1)]
    game.night_actions[2] = {"type": "investigate", "actor": 2, "role": "Sheriff", "target": 2}
    _atype, ids, _fmt = game._action_target_summary(game.night_actions[2], actor_id=2)
    assert ids == [3], "Ack should show transported house, not submitted slot"
    assert effective_primary_target(game, 2) == 3


async def scenario_blocked_sheriff_gets_investigate_interrupt() -> None:
    game, guild, members = make_game(seed=136, n=4)
    game.player_roles.update({1: "Sheriff", 2: "Escort", 3: "Mobster", 4: "Doctor"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Sheriff", "target": 3}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "could not investigate")
    assert "suspicious" not in "\n".join(members[1].inbox).lower()
    assert_post_night_invariants(game, out)


async def scenario_mole_consig_reveals_exact_role() -> None:
    game, guild, members = make_game(seed=137, n=3)
    game.player_roles.update({1: "Mole", 2: "Mobster", 3: "Doctor"})
    game.role_states[1] = {"uses_remaining": 1}
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Mole", "target": 2}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "Mobster")
    assert_post_night_invariants(game, out)


async def scenario_seer_gaze_two_town_reads_friends() -> None:
    game, guild, members = make_game(seed=138, n=5)
    game.player_roles.update({1: "Seer", 2: "Doctor", 3: "Sheriff", 4: "Mobster", 5: "Escort"})
    game.night_actions[1] = {"type": "gaze", "actor": 1, "role": "Seer", "targets": [2, 3]}
    out = await run_night_pipeline(game, guild)
    assert any("friend" in msg.lower() for msg in members[1].inbox), members[1].inbox
    assert_post_night_invariants(game, out)


async def scenario_investigator_frame_beats_douse() -> None:
    """Framed + doused town reads as Framer bucket, not Arsonist (engine tamper order)."""
    game, guild, members = make_game(seed=123, n=4)
    game.player_roles.update({1: "Framer", 2: "Arsonist", 3: "Investigator", 4: "Doctor"})
    game.role_states[1] = {}
    game.night_actions[1] = {"type": "frame", "actor": 1, "role": "Framer", "target": 4}
    game.night_actions[2] = {"type": "douse", "actor": 2, "role": "Arsonist", "target": 4}
    game.doused_players.add(4)
    game.night_actions[3] = {"type": "investigate", "actor": 3, "role": "Investigator", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[3], "Framer")
    assert "Arsonist" not in "\n".join(members[3].inbox), "Douse should not override frame for Investigator"
    assert_post_night_invariants(game, out)


async def scenario_framer_alters_investigations() -> None:
    # Sheriff sees framed target as suspicious.
    game, guild, members = make_game(seed=120, n=4)
    game.player_roles.update({1: "Framer", 2: "Sheriff", 3: "Doctor", 4: "Mobster"})
    game.night_actions[1] = {"type": "frame", "actor": 1, "role": "Framer", "target": 3}
    game.night_actions[2] = {"type": "investigate", "actor": 2, "role": "Sheriff", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[2], "suspicious")
    assert_post_night_invariants(game, out)

    # Investigator bucket includes Framer when framed.
    game2, guild2, members2 = make_game(seed=121, n=4)
    game2.player_roles.update({1: "Framer", 2: "Investigator", 3: "Doctor", 4: "Mobster"})
    game2.night_actions[1] = {"type": "frame", "actor": 1, "role": "Framer", "target": 3}
    game2.night_actions[2] = {"type": "investigate", "actor": 2, "role": "Investigator", "target": 3}
    out2 = await run_night_pipeline(game2, guild2)
    assert_dm_received(members2[2], "Framer")
    assert_post_night_invariants(game2, out2)


async def scenario_roleblocked_visitor_does_not_trigger_alert() -> None:
    game, guild, _members = make_game(seed=130, n=4)
    game.player_roles.update({1: "Scary Grandma", 2: "Vigilante", 3: "Escort", 4: "Doctor"})
    game.role_states[1] = {"alerts_remaining": 2}
    game.role_states[2] = {"shots_remaining": 1}
    game.night_actions[1] = {"type": "alert", "actor": 1, "role": "Scary Grandma"}
    game.night_actions[3] = {"type": "roleblock", "actor": 3, "role": "Escort", "target": 2}
    game.night_actions[2] = {"type": "shoot", "actor": 2, "role": "Vigilante", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 2 not in out["deaths"], "Blocked visitor should not trigger alert kill"
    assert_post_night_invariants(game, out)


async def scenario_gatekeeper_blocks_non_mafia_visitors() -> None:
    game, guild, _members = make_game(seed=140, n=5)
    game.player_roles.update({1: "Gatekeeper", 2: "Doctor", 3: "Mobster", 4: "Doctor", 5: "Townie"})
    game.role_states[1] = {"uses_remaining": 2}
    # Gatekeeper guards 4. Doctor 2 tries to heal 4; should be blocked by gatekeeper and not apply.
    game.night_actions[1] = {"type": "guard", "actor": 1, "role": "Gatekeeper", "target": 4}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 4}
    # Mobster attacks 4; if heal didn't apply, 4 should die.
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert game.night_actions.get(2, {}).get("blocked_by_gatekeeper") is True, "Expected Doctor to be marked blocked_by_gatekeeper"
    assert 4 in out["deaths"], "Gatekeeper-blocked Doctor should not save the target"
    assert_post_night_invariants(game, out)


async def scenario_revealed_mayor_cannot_be_healed() -> None:
    game, guild, _members = make_game(seed=150, n=4)
    game.player_roles.update({1: "Mayor", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"is_revealed": True}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 1}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 1 in out["deaths"], "Revealed Mayor should not be healable"
    assert_post_night_invariants(game, out)


async def scenario_witch_night1_shield() -> None:
    # Night 1: Witch survives first normal kill.
    game, guild, _members = make_game(seed=160, n=3)
    game.day_number = 1
    game.player_roles.update({1: "Witch", 2: "Mobster", 3: "Doctor"})
    game.role_states[1] = {"night1_shield_used": False, "has_learned_role": False}
    game.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 1 not in out["deaths"], "Witch should survive first Night 1 attack"
    assert game.role_states.get(1, {}).get("night1_shield_used") is True, "Witch shield should be marked used"
    assert game.role_states.get(1, {}).get("attacked_tonight_reason") == "witch_shield"
    assert_post_night_invariants(game, out)

    # Night 2+: Witch should die to normal kill.
    game2, guild2, _members2 = make_game(seed=161, n=3)
    game2.day_number = 2
    game2.player_roles.update({1: "Witch", 2: "Mobster", 3: "Doctor"})
    game2.role_states[1] = {"night1_shield_used": True, "has_learned_role": False}
    game2.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 1}
    out2 = await run_night_pipeline(game2, guild2)
    assert 1 in out2["deaths"], "Witch should not have shield after Night 1"
    assert_post_night_invariants(game2, out2)


async def scenario_jester_night1_shield() -> None:
    # Night 1: Jester survives first normal kill (same rule shape as Witch/Chaos).
    game, guild, _members = make_game(seed=162, n=3)
    game.day_number = 1
    game.player_roles.update({1: "Jester", 2: "Mobster", 3: "Doctor"})
    game.role_states[1] = {"night1_shield_used": False}
    game.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 1 not in out["deaths"], "Jester should survive first Night 1 attack"
    assert game.role_states.get(1, {}).get("night1_shield_used") is True, "Jester shield should be marked used"
    assert game.role_states.get(1, {}).get("attacked_tonight_reason") == "jester_shield"
    assert_post_night_invariants(game, out)

    game2, guild2, _members2 = make_game(seed=163, n=3)
    game2.day_number = 2
    game2.player_roles.update({1: "Jester", 2: "Mobster", 3: "Doctor"})
    game2.role_states[1] = {"night1_shield_used": True}
    game2.night_actions[2] = {"type": "kill", "actor": 2, "role": "Mobster", "target": 1}
    out2 = await run_night_pipeline(game2, guild2)
    assert 1 in out2["deaths"], "Jester should not have shield after Night 1"
    assert_post_night_invariants(game2, out2)


async def scenario_retributionist_reanimate_doctor_heals() -> None:
    game, guild, _members = make_game(seed=170, n=4)
    game.player_roles.update({1: "Retributionist", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"uses_remaining": 2, "used_corpses": []}
    # Mark doctor corpse in graveyard.
    game.graveyard = [{"player_id": 2, "real_role": "Doctor"}]
    game.living_players = [p for p in game.living_players if p.id != 2]
    # Retributionist reanimates doctor to heal 4; mobster attacks 4.
    game.night_actions[1] = {"type": "reanimate", "actor": 1, "role": "Retributionist", "corpse_player_id": 2, "corpse_role": "Doctor", "target": 4}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert 4 not in out["deaths"], "Reanimated Doctor heal should save the target"
    assert_post_night_invariants(game, out)


async def scenario_pirate_plunder_roleblocks_even_on_loss() -> None:
    game, guild, _members = make_game(seed=180, n=4)
    game.player_roles.update({1: "Pirate", 2: "Vigilante", 3: "Scary Grandma", 4: "Doctor"})
    game.role_states[2] = {"shots_remaining": 1}
    game.role_states[3] = {"alerts_remaining": 2}
    game.night_actions[3] = {"type": "alert", "actor": 3, "role": "Scary Grandma"}
    game.night_actions[1] = {
        "type": "plunder",
        "actor": 1,
        "role": "Pirate",
        "target": 2,
        "duel_won": False,
        "duel_finished": True,
    }
    game.night_actions[2] = {"type": "shoot", "actor": 2, "role": "Vigilante", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert 2 in out["blocked"], "Pirate plunder should roleblock even if duel is lost"
    assert 2 not in out["deaths"], "Roleblocked visitor should not trigger Grandma alert"
    assert_post_night_invariants(game, out)


async def scenario_pirate_win_requires_plunder_kill() -> None:
    """Duel win alone does not increment wins; target must die to pirate_plunder."""
    game, guild, _members = make_game(seed=181, n=4)
    game.player_roles.update({1: "Pirate", 2: "Doctor", 3: "Townie", 4: "Mobster"})
    game.role_states[1] = {"wins": 0}
    game.role_states[2] = {"self_heals_remaining": 1}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 3}
    game.night_actions[1] = {
        "type": "plunder",
        "actor": 1,
        "role": "Pirate",
        "target": 3,
        "duel_won": True,
        "duel_finished": True,
    }
    out = await run_night_pipeline(game, guild)
    assert 3 not in out["deaths"], "Doctor heal should stop a powerful plunder kill"
    assert int(game.role_states.get(1, {}).get("wins", 0)) == 0, "No win without a plunder kill"
    assert_post_night_invariants(game, out)

    game2, guild2, _members2 = make_game(seed=182, n=3)
    game2.player_roles.update({1: "Pirate", 2: "Townie", 3: "Mobster"})
    game2.role_states[1] = {"wins": 0}
    game2.night_actions[1] = {
        "type": "plunder",
        "actor": 1,
        "role": "Pirate",
        "target": 2,
        "duel_won": True,
        "duel_finished": True,
    }
    out2 = await run_night_pipeline(game2, guild2)
    assert 2 in out2["deaths"], "Plunder should kill an unarmored target when duel is won"
    assert game2.night_death_causes.get(2) == "pirate_plunder"
    assert int(game2.role_states.get(1, {}).get("wins", 0)) == 1, "Win counts only after plunder kill"
    assert_post_night_invariants(game2, out2)


async def scenario_first_healer_wins_on_duplicate_heal_target() -> None:
    """When two healers target the same player, the first processed healer wins (HEAL-001)."""
    game, guild, _members = make_game(seed=183, n=5)
    game.player_roles.update({1: "Doctor", 2: "Retributionist", 3: "Mobster", 4: "Townie", 5: "Townie"})
    game.role_states[1] = {"self_heals_remaining": 1}
    game.role_states[2] = {"uses_remaining": 2, "used_corpses": []}
    game.graveyard = [{"player_id": 99, "real_role": "Doctor"}]
    game.night_actions[1] = {"type": "heal", "actor": 1, "role": "Doctor", "target": 4}
    game.night_actions[2] = {
        "type": "reanimate",
        "actor": 2,
        "role": "Retributionist",
        "corpse_player_id": 99,
        "corpse_role": "Doctor",
        "target": 4,
    }
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert out["healed_by_map"].get(4) == 1, "Living Doctor should win the heal map slot"
    assert 4 not in out["deaths"], "First heal should still protect the target"
    assert_post_night_invariants(game, out)


async def scenario_lookout_dm_lists_visitors() -> None:
    game, guild, members = make_game(seed=200, n=5)
    game.player_roles.update({1: "Lookout", 2: "Doctor", 3: "Mobster", 4: "Townie", 5: "Townie"})
    # Doctor and Mobster both visit target 4.
    game.night_actions[1] = {"type": "watch", "actor": 1, "role": "Lookout", "target": 4}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 4}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    # Lookout DM should include visitor names (Doctor + Mobster).
    assert_dm_received(members[1], "P2")
    assert_dm_received(members[1], "P3")
    assert_post_night_invariants(game, out)


async def scenario_chain_roleblock_fixed_point() -> None:
    game, guild, _members = make_game(seed=210, n=4)
    # Fixed-point semantics test for resolve_blocking():
    # If a roleblocker is itself blocked, its block should not apply.
    #
    # Note: In this ruleset, real Escorts/Consorts are roleblock-immune, so we use synthetic
    # roleblock actions from non-immune roles to exercise the algorithm.
    game.player_roles.update({1: "Townie", 2: "Townie", 3: "Townie", 4: "Mobster"})
    # 1 attempts to block 4; 2 blocks 1 => 1 is blocked, so 4 should not be blocked by 1.
    game.night_actions[1] = {"type": "roleblock", "actor": 1, "role": "Townie", "target": 4}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Townie", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert 1 in out["blocked"], "Roleblocker 1 should be blocked"
    assert 4 not in out["blocked"], "Roleblock from a blocked roleblocker should not apply"
    assert_post_night_invariants(game, out)


async def scenario_broken_wrong_lookout_dm() -> None:
    """Fails on assert_dm_received (wrong visitor role in DM text)."""
    game, guild, members = make_game(seed=41_009, n=4)
    game.player_roles.update({1: "Lookout", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.night_actions[1] = {"type": "watch", "actor": 1, "role": "Lookout", "target": 2}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 2}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "visited by the Pirate")
    assert_post_night_invariants(game, out)


async def scenario_broken_healed_town_listed_as_dead() -> None:
    """Fails on night deaths assert (Doctor heal should prevent death)."""
    game, guild, _members = make_game(seed=41_010, n=4)
    game.player_roles.update({1: "Vigilante", 2: "Doctor", 3: "Mobster", 4: "Townie"})
    game.role_states[1] = {"shots_remaining": 1}
    game.night_actions[1] = {"type": "shoot", "actor": 1, "role": "Vigilante", "target": 2}
    game.night_actions[2] = {"type": "heal", "actor": 2, "role": "Doctor", "target": 2}
    out = await run_night_pipeline(game, guild)
    assert 2 in out["deaths"], "Healed Doctor should be in deaths (wrong)"
    assert_post_night_invariants(game, out)


async def scenario_broken_guilt_after_mafia_shot() -> None:
    """Fails on role_states assert (guilt applies to town kills, not mafia)."""
    game, guild, _members = make_game(seed=41_011, n=3)
    game.player_roles.update({1: "Vigilante", 2: "Mobster", 3: "Doctor"})
    game.role_states[1] = {"shots_remaining": 1}
    game.night_actions[1] = {"type": "shoot", "actor": 1, "role": "Vigilante", "target": 2}
    out = await run_night_pipeline(game, guild)
    assert game.role_states.get(1, {}).get("guilty_tomorrow") is True, "Vigilante should feel guilt after shooting Mafia (wrong)"
    assert_post_night_invariants(game, out)


async def scenario_broken_blocked_investigator_expects_bucket() -> None:
    """Fails on assert_dm_received (Escort block should interrupt, not return a bucket)."""
    game, guild, members = make_game(seed=41_012, n=4)
    game.player_roles.update({1: "Investigator", 2: "Escort", 3: "Doctor", 4: "Mobster"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Investigator", "target": 4}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "Your target could be a member of the Mafia")
    assert_post_night_invariants(game, out)


async def scenario_broken_roleblocked_bodyguard_saved_target() -> None:
    """Fails on deaths assert (blocked Bodyguard does not protect)."""
    game, guild, _members = make_game(seed=41_013, n=6)
    game.player_roles.update({1: "Bodyguard", 2: "Escort", 3: "Mobster", 4: "Doctor", 5: "Townie", 6: "Townie"})
    game.role_states[1] = {"uses_remaining": 1, "self_protects_remaining": 1}
    game.night_actions[1] = {"type": "protect", "actor": 1, "role": "Bodyguard", "target": 4}
    game.night_actions[2] = {"type": "roleblock", "actor": 2, "role": "Escort", "target": 1}
    game.night_actions[3] = {"type": "kill", "actor": 3, "role": "Mobster", "target": 4}
    out = await run_night_pipeline(game, guild)
    assert 4 not in out["deaths"], "Protected Doctor should survive Mafia kill (wrong)"
    assert_post_night_invariants(game, out)


async def scenario_broken_sheriff_expects_godfather_line() -> None:
    """Fails on assert_dm_received (Sheriff on Mobster is not a Godfather reveal)."""
    game, guild, members = make_game(seed=41_014, n=3)
    game.player_roles.update({1: "Sheriff", 2: "Doctor", 3: "Mobster"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Sheriff", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], "You investigated the Godfather")
    assert_post_night_invariants(game, out)


async def scenario_broken_mafia_kill_survived_wrong() -> None:
    """Fails on deaths assert (unhealed Townie should die)."""
    game, guild, _members = make_game(seed=41_015, n=4)
    game.player_roles.update({1: "Mobster", 2: "Doctor", 3: "Townie", 4: "Townie"})
    game.night_actions[1] = {"type": "kill", "actor": 1, "role": "Mobster", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert 3 not in out["deaths"], "Mafia kill should not kill unhealed Townie (wrong)"
    assert_post_night_invariants(game, out)


async def scenario_broken_post_night_invariants() -> None:
    """Fails on assert_post_night_invariants after corrupting death-cause bookkeeping."""
    game, guild, _members = make_game(seed=41_016, n=3)
    game.player_roles.update({1: "Mobster", 2: "Doctor", 3: "Townie"})
    game.night_actions[1] = {"type": "kill", "actor": 1, "role": "Mobster", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert 3 in out["deaths"]
    game.night_death_causes = {}
    assert_post_night_invariants(game, out)


# Deliberately failing scenarios — not in SCENARIO_FUNCTIONS. Use --broken-scenario / --broken-scenario-only.
BROKEN_SCENARIO_FUNCTIONS: Tuple[Callable[[], Any], ...] = (
    scenario_broken_wrong_lookout_dm,
    scenario_broken_healed_town_listed_as_dead,
    scenario_broken_guilt_after_mafia_shot,
    scenario_broken_blocked_investigator_expects_bucket,
    scenario_broken_roleblocked_bodyguard_saved_target,
    scenario_broken_sheriff_expects_godfather_line,
    scenario_broken_mafia_kill_survived_wrong,
    scenario_broken_post_night_invariants,
)


# Ordered behavioral regression suite (high-signal; run by default before fuzz/systematic).
SCENARIO_FUNCTIONS: Tuple[Callable[[], Any], ...] = (
    scenario_transport_redirects_target,
    scenario_control_immune_role_not_redirected,
    scenario_corrupted_actions_do_not_crash,
    scenario_gatekeeper_corrupted_guard_target_does_not_crash,
    scenario_graveyard_corruption_does_not_crash_sync,
    scenario_ignite_is_unstoppable_even_through_heal,
    scenario_transport_redirects_witch_control_targets,
    scenario_transport_does_not_redirect_self_only_actions,
    scenario_witch_cannot_retarget_self_only_actions,
    scenario_witch_can_prevent_arsonist_ignite_by_forcing_douse,
    scenario_arsonist_ignite_while_doused_kills_self,
    scenario_arsonist_basic_defense_survives_normal_kill,
    scenario_transport_does_not_redirect_pirate_plunder,
    scenario_witch_redirects_mafia_kill_target,
    scenario_executioner_converts_to_jester_on_target_non_lynch,
    scenario_executioner_marks_win_on_lynch,
    scenario_arsonist_clean_removes_douse,
    scenario_bodyguard_blocked_does_not_protect,
    scenario_vigilante_guilt_town_vs_mafia,
    scenario_blocked_investigator_gets_interrupt,
    scenario_blocked_tracker_gets_track_interrupt,
    scenario_blocked_lookout_gets_watch_interrupt,
    scenario_witch_receives_controlled_sheriff_result,
    scenario_witch_receives_controlled_investigator_bucket,
    scenario_mobster_investigator_protective_shield_bucket,
    scenario_chaos_records_visit_targets,
    scenario_corrupt_misc_snapshot_still_heals,
    scenario_investigative_checkpoint_no_duplicate_watch_dm,
    scenario_lookout_excludes_self_heal_from_visitors,
    scenario_witch_steals_psychic_vision,
    scenario_transport_ack_uses_effective_visit_house,
    scenario_blocked_sheriff_gets_investigate_interrupt,
    scenario_mole_consig_reveals_exact_role,
    scenario_seer_gaze_two_town_reads_friends,
    scenario_investigator_frame_beats_douse,
    scenario_framer_alters_investigations,
    scenario_roleblocked_visitor_does_not_trigger_alert,
    scenario_gatekeeper_blocks_non_mafia_visitors,
    scenario_revealed_mayor_cannot_be_healed,
    scenario_witch_night1_shield,
    scenario_jester_night1_shield,
    scenario_retributionist_reanimate_doctor_heals,
    scenario_pirate_plunder_roleblocks_even_on_loss,
    scenario_pirate_win_requires_plunder_kill,
    scenario_first_healer_wins_on_duplicate_heal_target,
    scenario_lookout_dm_lists_visitors,
    scenario_chain_roleblock_fixed_point,
)


def _scenario_list(*, include_broken: bool, broken_only: bool) -> Tuple[Callable[[], Any], ...]:
    if broken_only:
        return BROKEN_SCENARIO_FUNCTIONS
    if include_broken:
        return (*SCENARIO_FUNCTIONS, *BROKEN_SCENARIO_FUNCTIONS)
    return SCENARIO_FUNCTIONS


async def run_all_scenarios(*, include_broken: bool = False, broken_only: bool = False) -> int:
    fns = _scenario_list(include_broken=include_broken, broken_only=broken_only)
    for i, fn in enumerate(fns, start=1):
        name = getattr(fn, "__name__", repr(fn))
        print(f"scenario {i}/{len(fns)}: {name}...", flush=True)
        try:
            await fn()
        except Exception:
            print(f"scenario FAILED: {name}", flush=True)
            raise
    return len(fns)


async def _probe_invariant_failure() -> None:
    """Deliberately corrupt post-pipeline state; post-night invariants must reject it."""
    game, guild, _members = make_game(seed=99_991, n=3)
    game.player_roles.update({1: "Doctor", 2: "Mobster", 3: "Townie"})
    game.night_actions = {2: {"type": "kill", "actor": 2, "role": "Mobster", "target": 3}}
    out = await run_night_pipeline(game, guild)
    game.night_death_causes = {}
    try:
        assert_post_night_invariants(game, out)
    except AssertionError:
        return
    raise AssertionError("post_night_invariants should have failed on empty night_death_causes")


async def _probe_scenario_assertion_failure() -> None:
    """Scenario-style DM assert with impossible substring must fail."""
    game, guild, members = make_game(seed=99_992, n=3)
    game.player_roles.update({1: "Sheriff", 2: "Doctor", 3: "Mobster"})
    game.night_actions[1] = {"type": "investigate", "actor": 1, "role": "Sheriff", "target": 3}
    out = await run_night_pipeline(game, guild)
    try:
        assert_dm_received(members[1], "__SIM_PROBE_MISSING_DM__")
        assert_post_night_invariants(game, out)
    except AssertionError:
        return
    raise AssertionError("assert_dm_received should have failed on missing substring")


def _probe_worker_crash_task(_task: object) -> None:
    raise RuntimeError("sim_test probe: intentional systematic worker crash")


async def _probe_systematic_worker_failure() -> None:
    """Pool worker exceptions must surface to the parent process."""
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_probe_worker_crash_task, (None,))
            fut.result()
    except RuntimeError as e:
        if "intentional systematic worker crash" not in str(e):
            raise AssertionError(f"unexpected worker error: {e}") from e
        return
    raise AssertionError("expected worker crash")


async def _probe_systematic_coverage_propagates() -> None:
    """Serial systematic path must raise when a roleset run fails."""

    async def _boom(**_kwargs: object) -> SystematicRunStats:
        raise ValueError("sim_test probe: intentional roleset failure")

    import sys

    mod = sys.modules[__name__]
    real = mod._systematic_one_roleset
    mod._systematic_one_roleset = _boom  # type: ignore[assignment]
    try:
        try:
            await systematic_action_coverage(
                role_sets=[["Mobster", "Doctor", "Sheriff", "Townie", "Townie", "Townie", "Townie"]],
                jobs=1,
                save_repros=False,
                sample_per_combo=2,
                tuple_size=2,
            )
        except ValueError as e:
            if "intentional roleset failure" not in str(e):
                raise AssertionError(f"unexpected: {e}") from e
            return
        raise AssertionError("expected systematic_action_coverage to fail")
    finally:
        mod._systematic_one_roleset = real


async def run_failure_probes() -> None:
    checks = [
        ("post_night_invariants", _probe_invariant_failure),
        ("scenario_dm_assert", _probe_scenario_assertion_failure),
        ("systematic_worker", _probe_systematic_worker_failure),
        ("systematic_serial", _probe_systematic_coverage_propagates),
    ]
    for label, fn in checks:
        try:
            await fn()
        except AssertionError:
            print(f"probe {label}: sim harness caught AssertionError", flush=True)
            continue
        except (RuntimeError, ValueError) as e:
            if "intentional" in str(e) or "systematic worker failed" in str(e):
                print(f"probe {label}: sim harness caught {type(e).__name__}", flush=True)
                continue
            raise
        print(f"probe {label}: failure propagated as expected", flush=True)
    print("failure_probes: all checks behaved as expected", flush=True)


async def fuzz_night_actions_no_throw(*, iterations: int = 200, seed: int = 999) -> None:
    rng = random.Random(int(seed))
    game, guild, members = make_game(seed=int(seed) ^ 0xBEEF, n=8)
    # Give everyone a role so invariants can hold even as actions mutate.
    roles = ["Townie", "Doctor", "Escort", "Investigator", "Vigilante", "Lookout", "Tracker", "Transporter"]
    roles_by_seat = {i: r for i, r in enumerate(roles, start=1)}

    # Broaden fuzz coverage to most engine action types.
    action_types = [
        "heal",
        "roleblock",
        "investigate",
        "shoot",
        "watch",
        "track",
        "transport",
        "control",
        "douse",
        "ignite",
        "kill",
        "clean",
        "frame",
        "protect",
        "guard",
        "hypnotize",
        "tailor",
        "hide",
        "chaos",
        "vest",
        "alert",
        # Keep reanimate out of fuzz here; sim_test expands it and expects strict corpse metadata.
    ]
    for _ in range(iterations):
        reset_night_game_state(
            game,
            members=members,
            roles_by_seat=roles_by_seat,
            day_number=1,
        )
        # Randomly inject malformed and well-formed actions.
        for actor_id in range(1, 9):
            if rng.random() < 0.5:
                continue
            a_type = rng.choice(action_types)
            # Engine assumes `role` exists (injected by set_night_action()).
            payload: Dict[str, Any] = {"type": a_type, "actor": actor_id, "role": game.player_roles.get(actor_id, "Townie")}

            # Rough shapes. Occasionally corrupt them (higher corruption rate to shake loose edge crashes).
            corrupt = rng.random() < 0.35
            if a_type in {"transport", "control", "chaos"}:
                if corrupt:
                    payload["targets"] = [rng.choice(["x", None, 1.5, [], {}]), rng.choice(["y", [], {}, None])]
                else:
                    payload["targets"] = [rng.randint(1, 8), rng.randint(1, 8)]
            elif a_type in {"alert", "vest", "ignite"}:
                # self-only (usually) – sometimes add corrupted target anyway.
                if corrupt and rng.random() < 0.5:
                    payload["target"] = rng.choice(["bad", None, [], {}])
            else:
                # Most actions are single-target.
                payload["target"] = rng.choice([rng.randint(1, 8), "oops", None, [], {}]) if corrupt else rng.randint(1, 8)

            # Extra optional fields used by some actions.
            if a_type == "hypnotize":
                payload["msg_type"] = rng.choice(["healed", "roleblocked", "transported", "controlled", "attacked", None, 1])
            if a_type == "tailor":
                payload["fake_role"] = rng.choice(["Doctor", "Mobster", "Sheriff", None, 7])
            if a_type == "plunder":
                payload["duel_won"] = rng.choice([True, False, None, "x"])

            game.night_actions[actor_id] = payload

        out = await run_night_pipeline(game, guild)
        assert_post_night_invariants(game, out)


def _pools_for_player_count(player_count: int) -> Tuple[List[str], List[str], List[str], int, int, int]:
    """
    Enumerate exactly the same *role pools* and (mafia, neutral) counts as bot generation,
    but without any weighting/shuffling randomness.
    """
    num_mafia, num_neutral = mc_gen._bot_num_mafia_neutral(player_count)
    num_town = player_count - num_mafia - num_neutral
    town_pool = [r for r, _w in mc_gen._bot_town_weights(player_count)]
    mafia_support_pool = [r for r, _w in mc_gen._bot_mafia_support_weights(player_count)]
    # Full bracket display pool (deterministic); legality filtered via neutral_combo_draw_legal.
    pool = game_roles.start_pool_for_player_count(player_count, rng=random.Random(0))
    neutral_pool = list(pool.neutral_pool_for_display)
    return town_pool, mafia_support_pool, neutral_pool, num_town, num_mafia, num_neutral


def enumerate_all_role_sets(player_count: int) -> Iterable[List[str]]:
    """
    Enumerate all generator-legal role-sets for a given player_count, based on the same pools
    as `scripts/monte_carlo_sim.py` (which mirrors bot generation).
    """
    town_pool, mafia_support_pool, neutral_pool, num_town, num_mafia, num_neutral = _pools_for_player_count(player_count)

    # This bot's generator always includes exactly one Mobster.
    assert num_mafia >= 1, f"Unexpected mafia count: {num_mafia}"
    assert num_neutral >= 0, f"Unexpected neutral count: {num_neutral}"
    assert num_town >= 0, f"Unexpected town count: {num_town}"

    # Generator-legal role-sets for supported (num_mafia, num_neutral) brackets.
    support_count = max(0, num_mafia - 1)
    if support_count not in (0, 1):
        raise AssertionError(f"Unsupported mafia support count for enumeration: {support_count} (player_count={player_count})")
    if num_neutral not in (1, 2):
        raise AssertionError(f"Unsupported neutral count for enumeration: {num_neutral} (player_count={player_count})")

    if num_neutral == 1:
        neutral_iters: Iterable[List[str]] = (
            [n] for n in neutral_pool if game_roles.neutral_combo_draw_legal([n])
        )
    else:
        neutral_iters = (
            list(c)
            for c in itertools.combinations(neutral_pool, num_neutral)
            if game_roles.neutral_combo_draw_legal(c)
        )

    support_iters: Iterable[List[str]]
    if support_count == 0:
        support_iters = [[]]
    else:
        support_iters = ([s] for s in mafia_support_pool)

    for neutrals in neutral_iters:
        for mafia_support in support_iters:
            for town_roles in itertools.combinations(town_pool, num_town):
                roles = ["Mobster", *mafia_support, *neutrals, *town_roles]
                if len(roles) != player_count:
                    continue
                if len(set(roles)) != len(roles):
                    continue
                yield roles


TargetClass = str  # "self" | "other" | "town" | "mafia" | "neutral" | "any" | "invalid_list" | "invalid_dict" | "none"


def _role_faction(role: str) -> str:
    if role in bot_config.TOWN_ROLES:
        return "town"
    if role in bot_config.ALL_MAFIA_ROLES:
        return "mafia"
    return "neutral"


def _pick_target_id(
    *,
    actor_id: int,
    role_by_id: Dict[int, str],
    rng: random.Random,
    target_class: TargetClass,
) -> object:
    ids = list(role_by_id.keys())
    others = [i for i in ids if i != actor_id]
    if target_class == "none":
        return None
    if target_class == "invalid_list":
        return []
    if target_class == "invalid_dict":
        return {}
    if target_class == "self":
        return actor_id
    if target_class == "other":
        return rng.choice(others) if others else actor_id
    if target_class in {"town", "mafia", "neutral"}:
        pool = [i for i, r in role_by_id.items() if _role_faction(r) == target_class and i != actor_id]
        if not pool:
            return rng.choice(others) if others else actor_id
        return rng.choice(pool)
    # any
    return rng.choice(others) if others else actor_id


def _pick_two_targets(
    *,
    actor_id: int,
    role_by_id: Dict[int, str],
    rng: random.Random,
    a: TargetClass,
    b: TargetClass,
) -> List[object]:
    t1 = _pick_target_id(actor_id=actor_id, role_by_id=role_by_id, rng=rng, target_class=a)
    t2 = _pick_target_id(actor_id=actor_id, role_by_id=role_by_id, rng=rng, target_class=b)
    # If both are ints and equal, nudge t2 to a different other.
    if isinstance(t1, int) and isinstance(t2, int) and t1 == t2:
        others = [i for i in role_by_id.keys() if i not in {actor_id, t1}]
        if others:
            t2 = rng.choice(others)
    return [t1, t2]


def _action_for_role(role: str, *, rng: random.Random) -> Optional[Dict[str, Any]]:
    """
    Returns a plausible action payload for the role.
    This is intentionally simple: it's a crash-finder, not a balance model.
    """
    if role in {"Doctor"}:
        return {"type": "heal"}
    if role in {"Escort", "Consort"}:
        return {"type": "roleblock"}
    if role in {"Sheriff", "Investigator", "Mole"}:
        return {"type": "investigate"}
    if role in {"Vigilante"}:
        return {"type": "shoot"}
    if role in {"Mobster"}:
        return {"type": "kill"}
    if role in {"Lookout"}:
        return {"type": "watch"}
    if role in {"Tracker"}:
        return {"type": "track"}
    if role in {"Transporter"}:
        return {"type": "transport"}
    if role in {"Witch"}:
        return {"type": "control"}
    if role in {"Arsonist"}:
        return {"type": rng.choice(["douse", "ignite"])}
    if role in {"Pirate"}:
        # duel_won is used by killing; keep it random.
        return {"type": "plunder", "duel_won": bool(rng.getrandbits(1))}
    if role in {"Framer"}:
        return {"type": "frame"}
    if role in {"Bodyguard"}:
        return {"type": "protect"}
    if role in {"Scary Grandma"}:
        return {"type": "alert"}
    if role in {"Survivor"}:
        return {"type": "vest"}
    if role in {"Gatekeeper"}:
        return {"type": "guard"}
    if role in {"Guardian Angel"}:
        return {"type": "ward"}
    if role in {"Chaos"}:
        return {"type": "chaos"}
    if role in {"Serial Killer"}:
        return {"type": "sk_kill"}
    return None


def role_must_submit_night_action(role: str) -> bool:
    """True when the engine accepts a normal night command for this role (sim / bot)."""
    return _action_for_role(role, rng=random.Random(0)) is not None


def _build_mandatory_night_action(
    *,
    actor_id: int,
    role: str,
    role_by_id: Dict[int, str],
    role_states: Dict[int, Dict[str, Any]],
    rng: random.Random,
) -> Optional[Dict[str, Any]]:
    """Build a night_actions payload for roles that should act; None for passive roles."""
    act = _action_for_role(role, rng=rng)
    if act is None:
        return None

    payload: Dict[str, Any] = {"actor": actor_id, "role": role, **act}
    ids = list(role_by_id.keys())
    others = [i for i in ids if i != actor_id]
    target = rng.choice(others) if others else actor_id
    t1, t2 = rng.sample(ids, 2) if len(ids) >= 2 else (actor_id, actor_id)

    a_type = payload["type"]
    if a_type in {"transport", "control", "chaos"}:
        payload["targets"] = [t1, t2]
    elif a_type == "alert":
        payload["target"] = actor_id
    elif a_type == "vest":
        payload.pop("target", None)
    elif a_type == "ward":
        bind = role_states.get(actor_id, {}).get("ga_target_id")
        payload["target"] = int(bind) if bind is not None else target
    else:
        payload["target"] = target

    if a_type == "plunder":
        payload["duel_finished"] = True
        payload.setdefault("duel_won", bool(rng.getrandbits(1)))
    if a_type == "sk_kill":
        payload.setdefault("target", target)

    return payload


def _fill_mandatory_night_actions(
    game: Game,
    *,
    role_by_id: Dict[int, str],
    rng: random.Random,
    skip_seats: Optional[Set[int]] = None,
) -> None:
    """Ensure every role with a night command submits an action (realistic full-lobby nights)."""
    skip = skip_seats or set()
    for seat, role in role_by_id.items():
        if seat in skip or seat in game.night_actions:
            continue
        if not role_must_submit_night_action(role):
            continue
        payload = _build_mandatory_night_action(
            actor_id=seat,
            role=role,
            role_by_id=role_by_id,
            role_states=game.role_states,
            rng=rng,
        )
        if payload is not None:
            game.night_actions[seat] = payload


def _systematic_variants_for_role(role: str) -> List[Dict[str, Any]]:
    """
    Small, meaningful action-variant set per role.
    Each variant is a partial payload with `type` and target-class hints.
    """
    if role == "Doctor":
        return [
            {"type": "heal", "target_class": "self"},
            {"type": "heal", "target_class": "town"},
            {"type": "heal", "target_class": "other"},
            # Should be ignored safely if malformed/None in engine.
            {"type": "heal", "target_class": "none"},
        ]
    if role in {"Escort", "Consort"}:
        return [
            {"type": "roleblock", "target_class": "mafia"},
            {"type": "roleblock", "target_class": "town"},
            {"type": "roleblock", "target_class": "other"},
            # Self-target is illegal in many rulesets; should be tolerated.
            {"type": "roleblock", "target_class": "self"},
        ]
    if role in {"Sheriff", "Investigator", "Mole"}:
        return [
            {"type": "investigate", "target_class": "mafia"},
            {"type": "investigate", "target_class": "neutral"},
            {"type": "investigate", "target_class": "town"},
            {"type": "investigate", "target_class": "other"},
        ]
    if role == "Lookout":
        return [
            {"type": "watch", "target_class": "mafia"},
            {"type": "watch", "target_class": "town"},
            {"type": "watch", "target_class": "neutral"},
        ]
    if role == "Tracker":
        return [
            {"type": "track", "target_class": "mafia"},
            {"type": "track", "target_class": "town"},
            {"type": "track", "target_class": "neutral"},
        ]
    if role == "Transporter":
        return [
            {"type": "transport", "targets_classes": ("town", "mafia")},
            {"type": "transport", "targets_classes": ("town", "town")},
            {"type": "transport", "targets_classes": ("neutral", "town")},
            {"type": "transport", "targets_classes": ("other", "other")},
            # Same-target attempt: should not crash (even if rejected in commands).
            {"type": "transport", "targets_classes": ("town", "town"), "allow_same": True},
        ]
    if role == "Witch":
        return [
            {"type": "control", "targets_classes": ("mafia", "town")},
            {"type": "control", "targets_classes": ("town", "mafia")},
            {"type": "control", "targets_classes": ("neutral", "town")},
            {"type": "control", "targets_classes": ("other", "other")},
        ]
    if role == "Vigilante":
        return [
            {"type": "shoot", "target_class": "mafia"},
            {"type": "shoot", "target_class": "town"},
            {"type": "shoot", "target_class": "neutral"},
            {"type": "shoot", "target_class": "self"},
        ]
    if role == "Mobster":
        return [
            {"type": "kill", "target_class": "town"},
            {"type": "kill", "target_class": "neutral"},
            {"type": "kill", "target_class": "self"},
        ]
    if role == "Scary Grandma":
        return [{"type": "alert"}]
    if role == "Arsonist":
        return [
            {"type": "douse", "target_class": "town"},
            {"type": "douse", "target_class": "mafia"},
            {"type": "douse", "target_class": "neutral"},
            {"type": "douse", "target_class": "self"},
            {"type": "ignite"},
            {"type": "clean"},
        ]
    if role == "Pirate":
        return [
            {"type": "plunder", "target_class": "town", "duel_won": True},
            {"type": "plunder", "target_class": "town", "duel_won": False},
            {"type": "plunder", "target_class": "mafia", "duel_won": True},
            {"type": "plunder", "target_class": "neutral", "duel_won": False},
        ]
    if role == "Framer":
        return [
            {"type": "frame", "target_class": "town"},
            {"type": "frame", "target_class": "neutral"},
        ]
    if role in {"Bodyguard"}:
        return [
            {"type": "protect", "target_class": "self"},
            {"type": "protect", "target_class": "town"},
            {"type": "protect", "target_class": "neutral"},
        ]
    if role in {"Survivor"}:
        return [{"type": "vest"}]
    if role in {"Gatekeeper"}:
        return [{"type": "guard", "target_class": "town"}, {"type": "guard", "target_class": "other"}]
    if role == "Seer":
        return [
            {"type": "gaze", "targets_classes": ("town", "town")},
            {"type": "gaze", "targets_classes": ("mafia", "town")},
            {"type": "gaze", "targets_classes": ("neutral", "town")},
        ]
    if role == "Chaos":
        return [
            {"type": "chaos", "targets_classes": ("town", "mafia")},
            {"type": "chaos", "targets_classes": ("town", "town")},
            {"type": "chaos", "targets_classes": ("other", "other")},
        ]
    # For roles that don't participate in night engine (Mayor/Executioner/Jester/etc),
    # "no action" is enough for this sim.
    return []


def _corruption_variants_for_role(role: str) -> List[Dict[str, Any]]:
    """
    A tiny set of intentionally malformed payloads per role to ensure the engine stays tolerant.
    Keep this small so systematic runs stay fast.
    """
    # Provide a couple malformed target shapes for single-target actions.
    single_target_type_for_role = {
        "Doctor": "heal",
        "Escort": "roleblock",
        "Consort": "roleblock",
        "Sheriff": "investigate",
        "Investigator": "investigate",
        "Mole": "investigate",
        "Lookout": "watch",
        "Tracker": "track",
        "Vigilante": "shoot",
        "Mobster": "kill",
        "Arsonist": "douse",
        "Pirate": "plunder",
        "Framer": "frame",
        "Bodyguard": "protect",
    }
    if role in single_target_type_for_role:
        t = single_target_type_for_role[role]
        extra: Dict[str, Any] = {}
        if role == "Pirate":
            extra["duel_won"] = True
        return [
            {"type": t, "target_class": "invalid_list", **extra},
            {"type": t, "target_class": "invalid_dict", **extra},
            {"type": t, "target_class": "none", **extra},
        ]
    if role in {"Transporter", "Witch"}:
        return [
            {"type": "transport" if role == "Transporter" else "control", "targets_classes": ("invalid_list", "town")},
            {"type": "transport" if role == "Transporter" else "control", "targets_classes": ("invalid_dict", "mafia")},
            {"type": "transport" if role == "Transporter" else "control", "targets_classes": ("none", "none")},
        ]
    return []


def assert_dm_received(member: FakeMember, substring: str) -> None:
    assert any(substring in msg for msg in member.inbox), f"Expected DM containing {substring!r}, got: {member.inbox}"


def _materialize_action(
    *,
    actor_id: int,
    actor_role: str,
    role_by_id: Dict[int, str],
    rng: random.Random,
    variant: Dict[str, Any],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"actor": actor_id, "role": actor_role, "type": variant["type"]}
    if "duel_won" in variant:
        payload["duel_won"] = bool(variant["duel_won"])

    if "target_class" in variant:
        payload["target"] = _pick_target_id(actor_id=actor_id, role_by_id=role_by_id, rng=rng, target_class=str(variant["target_class"]))
    if "targets_classes" in variant:
        a, b = variant["targets_classes"]
        payload["targets"] = _pick_two_targets(actor_id=actor_id, role_by_id=role_by_id, rng=rng, a=str(a), b=str(b))
        if variant.get("allow_same") and isinstance(payload["targets"][0], int):
            # Force same-target attempt.
            payload["targets"][1] = payload["targets"][0]
    return payload


def _init_state_for_role(role: str) -> Dict[str, Any]:
    # Minimal counters used by engine logic; not intended to model full gameplay.
    if role == "Vigilante":
        return {"shots_remaining": 1}
    if role == "Survivor":
        return {"vests_remaining": 2}
    if role == "Doctor":
        return {"self_heals_remaining": 1}
    if role == "Bodyguard":
        return {"uses_remaining": 1, "self_protects_remaining": 1}
    if role == "Scary Grandma":
        return {"alerts_remaining": 2}
    if role == "Retributionist":
        return {"uses_remaining": 2, "used_corpses": []}
    if role == "Chaos":
        return {"uses_remaining": 2, "night1_shield_used": False}
    if role == "Witch":
        return {"night1_shield_used": False, "has_learned_role": False}
    if role == "Jester":
        return {"night1_shield_used": False}
    if role == "Mole":
        return {"uses_remaining": 1}
    if role == "Gatekeeper":
        return {"uses_remaining": 2}
    return {}


def _variant_tuple_key(variant_tuple: Tuple[Dict[str, Any], ...]) -> str:
    return json.dumps(list(variant_tuple), sort_keys=True, default=str)


def _iter_variant_tuples(
    variant_lists: List[List[Dict[str, Any]]],
    *,
    combo_rng: random.Random,
    sample_per_combo: int,
) -> Iterable[Tuple[Dict[str, Any], ...]]:
    """Full cartesian when sample_per_combo <= 0; else K deterministic random variant draws per seat combo."""
    if sample_per_combo <= 0:
        yield from itertools.product(*variant_lists)
        return
    seen: Set[str] = set()
    attempts = 0
    cap = max(sample_per_combo * 30, sample_per_combo)
    while len(seen) < sample_per_combo and attempts < cap:
        attempts += 1
        picked = tuple(combo_rng.choice(vl) for vl in variant_lists)
        key = _variant_tuple_key(picked)
        if key in seen:
            continue
        seen.add(key)
        yield picked


async def _exhaustive_one_roleset(
    *,
    rs_i: int,
    roles: List[str],
    player_count: int,
    per_combo_nights: int,
    seed: int,
    save_repros: bool,
) -> None:
    rng = random.Random(int(seed) + int(rs_i) * 7_919)
    game, guild, _members = make_game(seed=int(seed) + int(rs_i), n=player_count)
    for seat, role in enumerate(roles, start=1):
        game.player_roles[seat] = role
        game.role_states.setdefault(seat, dict(_init_state_for_role(role)))

    roles_by_seat = {seat: game.player_roles[seat] for seat in range(1, player_count + 1)}

    for _night in range(per_combo_nights):
        reset_night_game_state(
            game,
            members=_members,
            roles_by_seat=roles_by_seat,
            day_number=1,
        )
        for actor_id in range(1, player_count + 1):
            role = game.player_roles[actor_id]
            act = _action_for_role(role, rng=rng)
            if act is None:
                continue
            if rng.random() < 0.35:
                continue

            payload: Dict[str, Any] = {"actor": actor_id, "role": role, **act}

            if payload["type"] in {"transport", "control", "chaos"}:
                if rng.random() < 0.03:
                    payload["targets"] = [rng.choice([[], {}, "x", None]), rng.choice([[], {}, "y", None])]
                else:
                    a = rng.randint(1, player_count)
                    b = rng.randint(1, player_count)
                    payload["targets"] = [a, b]
            elif payload["type"] in {"ignite"}:
                if rng.random() < 0.03:
                    payload["target"] = rng.choice([[], {}, "bad", None])
            else:
                if rng.random() < 0.03:
                    payload["target"] = rng.choice([[], {}, "oops", None])
                else:
                    payload["target"] = rng.randint(1, player_count)

            game.night_actions[actor_id] = payload

        out = await run_night_pipeline(game, guild)
        assert_post_night_invariants(game, out)


def _exhaustive_roleset_worker(
    task: Tuple[int, List[str], int, int, int, bool],
) -> Tuple[int, Optional[str]]:
    rs_i, roles, player_count, per_combo_nights, seed, save_repros = task
    try:
        asyncio.run(
            _exhaustive_one_roleset(
                rs_i=rs_i,
                roles=roles,
                player_count=player_count,
                per_combo_nights=per_combo_nights,
                seed=seed,
                save_repros=save_repros,
            )
        )
        return rs_i, None
    except Exception:
        return rs_i, traceback.format_exc()


async def exhaustive_role_combos_no_crash(
    *,
    player_count: int,
    per_combo_nights: int = 2,
    seed: int = 123,
    jobs: Optional[int] = None,
    save_repros: bool = True,
) -> None:
    """
    For every generator-legal role-set at player_count, run randomized nights through the real engine.
    Goal: ensure the night pipeline never throws across the bracket role-space.
    """
    combos = list(enumerate_all_role_sets(player_count))
    total = len(combos)

    assert 10_000 <= total <= 250_000, f"Enumeration size unexpected: player_count={player_count} total={total}"

    n_jobs = 1 if jobs == 1 else max(1, min(int(jobs or 1), total))

    if n_jobs <= 1:
        for idx, roles in enumerate(combos, start=1):
            try:
                await _exhaustive_one_roleset(
                    rs_i=idx,
                    roles=roles,
                    player_count=player_count,
                    per_combo_nights=per_combo_nights,
                    seed=seed,
                    save_repros=save_repros,
                )
            except Exception as e:
                if save_repros:
                    repro = {
                        "kind": "exhaustive_role_combos_no_crash",
                        "player_count": player_count,
                        "seed": seed,
                        "roles": roles,
                        "roleset_index": idx,
                        "exception": {"type": type(e).__name__, "message": str(e)},
                    }
                    p = _write_repro(name=f"exhaustive{player_count}p", payload=repro)
                    print(f"WROTE REPRO: {p}", flush=True)
                raise
            if idx % 2000 == 0:
                print(f"exhaustive_{player_count}p: {idx}/{total} role-sets OK...", flush=True)
        return

    tasks = [
        (idx, roles, player_count, per_combo_nights, seed, save_repros)
        for idx, roles in enumerate(combos, start=1)
    ]
    done = 0
    with ProcessPoolExecutor(max_workers=n_jobs) as pool:
        futures = [pool.submit(_exhaustive_roleset_worker, t) for t in tasks]
        for fut in as_completed(futures):
            rs_i, err = fut.result()
            done += 1
            if err:
                print(f"CRASH exhaustive roleset_index={rs_i}\n{err[:2000]}", flush=True)
                raise RuntimeError(f"exhaustive worker failed at roleset_index={rs_i}")
            if done % 2000 == 0:
                print(f"exhaustive_{player_count}p: {done}/{total} role-sets OK...", flush=True)


async def _systematic_one_roleset(
    *,
    rs_i: int,
    roles: List[str],
    seed: int,
    per_roleset_pair_samples: int,
    save_repros: bool,
    dedupe_actions: bool,
    tuple_size: int = 2,
    all_roles_act: bool = True,
    sample_per_combo: int = 0,
) -> SystematicRunStats:
    stats = SystematicRunStats()
    if tuple_size < 2 or tuple_size > len(roles):
        raise ValueError(f"tuple_size must be 2..{len(roles)}, got {tuple_size}")

    game, guild, members = make_game(seed=seed + rs_i, n=len(roles))
    role_by_id = {seat: role for seat, role in enumerate(roles, start=1)}
    roles_by_seat = dict(role_by_id)
    seat_ids = list(range(1, len(roles) + 1))
    seat_combos = list(itertools.combinations(seat_ids, tuple_size))

    for seat_combo in seat_combos:
        combo_roles = [role_by_id[sid] for sid in seat_combo]
        variant_lists = [_variants_for_role(r) for r in combo_roles]
        seen_keys: Set[str] = set()

        for _sample in range(per_roleset_pair_samples):
            combo_rng = random.Random(
                _materialize_combo_seed(seed, rs_i, seat_combo, ())
                + _sample * 1_048_583
            )
            for variant_tuple in _iter_variant_tuples(
                variant_lists, combo_rng=combo_rng, sample_per_combo=int(sample_per_combo)
            ):
                reset_night_game_state(
                    game,
                    members=members,
                    roles_by_seat=roles_by_seat,
                    day_number=1,
                )
                night_rng = random.Random(
                    _materialize_combo_seed(seed, rs_i, seat_combo, variant_tuple)
                    + _sample * 1_048_583
                )
                game.night_actions.clear()

                variant_by_seat = dict(zip(seat_combo, variant_tuple))
                for sid, variant in variant_by_seat.items():
                    if variant.get("type") == "noop":
                        continue
                    game.night_actions[sid] = _materialize_action(
                        actor_id=sid,
                        actor_role=role_by_id[sid],
                        role_by_id=role_by_id,
                        rng=night_rng,
                        variant=variant,
                    )

                if all_roles_act:
                    _fill_mandatory_night_actions(
                        game,
                        role_by_id=role_by_id,
                        rng=night_rng,
                        skip_seats=set(),
                    )

                if dedupe_actions and game.night_actions:
                    action_key = _canonical_night_actions_key(game.night_actions)
                    if action_key in seen_keys:
                        stats.nights_deduped += 1
                        continue
                    seen_keys.add(action_key)

                try:
                    out = await run_night_pipeline(game, guild)
                    assert_post_night_invariants(game, out)
                    stats.nights_executed += 1
                except Exception as e:
                    if save_repros:
                        repro = {
                            "kind": "systematic_action_coverage",
                            "seed": seed,
                            "roles": roles,
                            "roleset_index": rs_i,
                            "tuple_size": tuple_size,
                            "seats": list(seat_combo),
                            "roles_by_seat": {str(s): role_by_id[s] for s in seat_combo},
                            "variants_by_seat": {
                                str(s): variant_by_seat[s] for s in seat_combo
                            },
                            "night_actions": {str(k): v for k, v in game.night_actions.items()},
                            "exception": {"type": type(e).__name__, "message": str(e)},
                        }
                        p = _write_repro(name="systematic", payload=repro)
                        print(f"WROTE REPRO: {p}", flush=True)
                    raise e

    return stats


def _systematic_roleset_worker(
    task: Tuple[int, List[str], int, int, bool, bool, int, bool, int],
) -> Tuple[int, Optional[str], SystematicRunStats]:
    (
        rs_i,
        roles,
        seed,
        per_roleset_pair_samples,
        save_repros,
        dedupe_actions,
        tuple_size,
        all_roles_act,
        sample_per_combo,
    ) = task
    try:
        stats = asyncio.run(
            _systematic_one_roleset(
                rs_i=rs_i,
                roles=roles,
                seed=seed,
                per_roleset_pair_samples=per_roleset_pair_samples,
                save_repros=save_repros,
                dedupe_actions=dedupe_actions,
                tuple_size=tuple_size,
                all_roles_act=all_roles_act,
                sample_per_combo=sample_per_combo,
            )
        )
        return rs_i, None, stats
    except Exception:
        return rs_i, traceback.format_exc(), SystematicRunStats()


async def systematic_action_coverage(
    *,
    role_sets: Iterable[List[str]],
    seed: int = 777,
    per_roleset_pair_samples: int = 1,
    save_repros: bool = True,
    dedupe_actions: bool = True,
    jobs: Optional[int] = None,
    tuple_size: int = 2,
    all_roles_act: bool = True,
    sample_per_combo: int = 0,
) -> SystematicRunStats:
    """
    Systematic action coverage per role-set and seat tuple.

    ``sample_per_combo`` <= 0: full cartesian product of per-role variants.
    ``sample_per_combo`` > 0: K random variant tuples per seat combo (seeded, deduped).

    When ``all_roles_act`` is True (default), other seats also submit mandatory actions.
    """
    role_sets_list = list(role_sets)
    total_stats = SystematicRunStats()
    mode = f"sample{sample_per_combo}" if sample_per_combo > 0 else "full"
    label = f"{tuple_size}-way-{mode}"
    n_jobs = 1 if jobs == 1 else max(1, min(int(jobs or default_trial_workers(len(role_sets_list))), len(role_sets_list)))

    if n_jobs <= 1:
        for rs_i, roles in enumerate(role_sets_list, start=1):
            stats = await _systematic_one_roleset(
                rs_i=rs_i,
                roles=roles,
                seed=seed,
                per_roleset_pair_samples=per_roleset_pair_samples,
                save_repros=save_repros,
                dedupe_actions=dedupe_actions,
                tuple_size=tuple_size,
                all_roles_act=all_roles_act,
                sample_per_combo=sample_per_combo,
            )
            total_stats.nights_executed += stats.nights_executed
            total_stats.nights_deduped += stats.nights_deduped
            if rs_i % 50 == 0:
                print(
                    f"systematic_{label}: {rs_i}/{len(role_sets_list)} role-sets OK...",
                    flush=True,
                )
        return total_stats

    tasks = [
        (
            rs_i,
            roles,
            seed,
            per_roleset_pair_samples,
            save_repros,
            dedupe_actions,
            tuple_size,
            all_roles_act,
            sample_per_combo,
        )
        for rs_i, roles in enumerate(role_sets_list, start=1)
    ]
    done = 0
    with ProcessPoolExecutor(max_workers=n_jobs) as pool:
        futures = [pool.submit(_systematic_roleset_worker, t) for t in tasks]
        for fut in as_completed(futures):
            rs_i, err, stats = fut.result()
            done += 1
            if err:
                print(f"CRASH roleset_index={rs_i}\n{err[:2000]}", flush=True)
                raise RuntimeError(f"systematic worker failed at roleset_index={rs_i}")
            total_stats.nights_executed += stats.nights_executed
            total_stats.nights_deduped += stats.nights_deduped
            if done % 50 == 0:
                print(
                    f"systematic_{label}: {done}/{len(role_sets_list)} role-sets OK...",
                    flush=True,
                )
    return total_stats


async def main() -> None:
    ap = argparse.ArgumentParser(description="Behavioral simulation tests for the real night engine.")
    ap.add_argument("--player-count", type=int, default=7, help="Player count to enumerate/sweep (supported: 7-9).")
    ap.add_argument("--seed", type=int, default=12345, help="Seed for fuzz/exhaustive/systematic runs.")
    ap.add_argument("--skip-scenarios", action="store_true", help="Skip the deterministic scenario suite (useful for tight loops).")
    ap.add_argument("--fuzz-iterations", type=int, default=200, help="How many fuzz iterations to run.")
    ap.add_argument("--skip-fuzz", action="store_true", help="Skip fuzz_night_actions_no_throw.")
    ap.add_argument("--skip-exhaustive", action="store_true", help="Skip exhaustive role-set enumeration.")
    ap.add_argument(
        "--exhaustive-nights",
        type=int,
        default=2,
        help="Random nights per legal role-set in exhaustive sweep (default 2).",
    )
    ap.add_argument("--systematic-actions", action="store_true", help="Enable systematic pairwise action-variant coverage.")
    ap.add_argument(
        "--systematic-role-sets",
        type=int,
        default=0,
        help="How many role-sets to run systematic actions on (0 = all role-sets for player-count).",
    )
    ap.add_argument("--systematic-pair-samples", type=int, default=1, help="Repeat each seat-pair scenario set this many times.")
    ap.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="Parallel worker processes for --systematic-actions (default: CPU count). Use 1 or --serial for single-process.",
    )
    ap.add_argument(
        "--serial",
        action="store_true",
        help="Run --systematic-actions in a single process (same as --jobs 1).",
    )
    ap.add_argument(
        "--no-dedupe-actions",
        action="store_true",
        help="Disable skipping identical materialized night_actions within each seat-pair.",
    )
    ap.add_argument(
        "--systematic-tuple-size",
        type=int,
        default=2,
        choices=(2, 3, 4, 5),
        help="Seat tuple size for --systematic-actions (2=pairwise … 5=penta).",
    )
    ap.add_argument(
        "--isolated-systematic",
        action="store_true",
        help="Only the focal tuple acts (legacy pairwise isolation). Disables mandatory all-role actions.",
    )
    ap.add_argument(
        "--systematic-sample",
        type=int,
        default=0,
        metavar="K",
        help="If K>0, run K random variant tuples per seat combo instead of full cartesian product.",
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Power deep 7p run (~30 min parallel, 12 jobs): 47 scenarios, fuzz 400, exhaustive "
            "all 47,775 lineups x5 nights, sampled 2/3/4-way systematic (~10M+ pipeline nights)."
        ),
    )
    ap.add_argument(
        "--quad",
        action="store_true",
        help=(
            "Bounded 4-way systematic run: enables --systematic-actions with tuple size 4, "
            "skips fuzz/exhaustive, uses 8 players and 4 role-sets unless overridden."
        ),
    )
    ap.add_argument(
        "--penta",
        action="store_true",
        help=(
            "Bounded 5-way systematic run: enables --systematic-actions with tuple size 5, "
            "skips fuzz/exhaustive, uses 8 players and 4 role-sets unless overridden (~35 min)."
        ),
    )
    ap.add_argument(
        "--scenarios-only",
        action="store_true",
        help="Run only the behavioral scenario suite (skip fuzz, exhaustive, systematic).",
    )
    ap.add_argument(
        "--probe-failure",
        action="store_true",
        help="Run built-in failure probes (verify asserts/ worker errors propagate); then exit.",
    )
    ap.add_argument(
        "--broken-scenario",
        action="store_true",
        help=f"After the normal suite, run {len(BROKEN_SCENARIO_FUNCTIONS)} deliberate failing scenarios.",
    )
    ap.add_argument(
        "--broken-scenario-only",
        action="store_true",
        help=f"Run only the {len(BROKEN_SCENARIO_FUNCTIONS)} deliberate failing scenarios (stops on first failure).",
    )
    args = ap.parse_args()

    if args.probe_failure:
        await run_failure_probes()
        print("sim_test.py: failure detection verified", flush=True)
        return

    if args.scenarios_only:
        args.skip_fuzz = True
        args.skip_exhaustive = True
        args.systematic_actions = False

    if args.broken_scenario_only:
        args.skip_fuzz = True
        args.skip_exhaustive = True
        args.systematic_actions = False

    if args.quad:
        args.systematic_actions = True
        args.systematic_tuple_size = 4
        args.skip_fuzz = True
        args.skip_exhaustive = True
        if int(args.player_count) == 7:
            args.player_count = 8
        if int(args.systematic_role_sets) == 0:
            args.systematic_role_sets = 4
        # Avoid multi-process state-file races when not explicitly parallelized.
        if not args.serial and args.jobs is None:
            args.serial = True

    if args.penta:
        args.systematic_actions = True
        args.systematic_tuple_size = 5
        args.skip_fuzz = True
        args.skip_exhaustive = True
        if int(args.player_count) == 7:
            args.player_count = 8
        if int(args.systematic_role_sets) == 0:
            args.systematic_role_sets = 4
        if not args.serial and args.jobs is None:
            args.serial = True

    deep_systematic_plan: List[Tuple[int, int, int]] = []
    if args.deep:
        args.player_count = 7
        args.skip_fuzz = False
        args.fuzz_iterations = max(int(args.fuzz_iterations), 400)
        args.exhaustive_nights = max(int(args.exhaustive_nights), 5)
        args.systematic_actions = True
        args.systematic_sample = 0
        # (tuple_size, role_sets_cap, samples_per_seat_combo)
        deep_systematic_plan = [(2, 9000, 56), (3, 2200, 20), (4, 1200, 14)]
        n_7p_lineups = len(list(enumerate_all_role_sets(7)))
        est_systematic = sum(
            min(rs, n_7p_lineups) * math.comb(7, ts) * k for ts, rs, k in deep_systematic_plan
        )
        print(
            f"deep plan: scenarios=47, fuzz>={args.fuzz_iterations}, "
            f"exhaustive={n_7p_lineups}x{int(args.exhaustive_nights)} nights, "
            f"systematic~{est_systematic:,} upper-bound pipeline nights (parallel)...",
            flush=True,
        )

    n_scenarios = 0
    if not args.skip_scenarios:
        n_scenarios = await run_all_scenarios(
            include_broken=bool(args.broken_scenario),
            broken_only=bool(args.broken_scenario_only),
        )
        print(f"scenarios: {n_scenarios} passed", flush=True)

    if not args.skip_fuzz:
        await fuzz_night_actions_no_throw(iterations=int(args.fuzz_iterations), seed=int(args.seed))

    if not args.skip_exhaustive:
        combos_list = list(enumerate_all_role_sets(int(args.player_count)))
        n_exhaustive = len(combos_list)
        if args.deep and not args.serial and args.jobs is None:
            ex_jobs = default_trial_workers(n_exhaustive)
        elif args.jobs is not None:
            ex_jobs = max(1, int(args.jobs))
        else:
            ex_jobs = 1 if args.serial else default_trial_workers(n_exhaustive)
        print(
            f"exhaustive: {n_exhaustive} role-sets x {int(args.exhaustive_nights)} nights "
            f"(random, jobs={ex_jobs})...",
            flush=True,
        )
        await exhaustive_role_combos_no_crash(
            player_count=int(args.player_count),
            per_combo_nights=int(args.exhaustive_nights),
            seed=int(args.seed),
            jobs=ex_jobs,
        )
        print("exhaustive: OK", flush=True)

    if args.systematic_actions:
        plans: List[Tuple[int, int, int]] = deep_systematic_plan or [
            (int(args.systematic_tuple_size), int(args.systematic_role_sets), int(args.systematic_sample))
        ]
        for tuple_size, role_limit, sample_k in plans:
            limit = int(role_limit)
            rolesets_iter: Iterable[List[str]] = enumerate_all_role_sets(int(args.player_count))
            if limit > 0:
                rolesets_iter = itertools.islice(rolesets_iter, limit)
            rolesets_list = list(rolesets_iter)
            if args.serial:
                n_jobs = 1
            elif args.jobs is not None:
                n_jobs = max(1, int(args.jobs))
            else:
                n_jobs = default_trial_workers(len(rolesets_list))
            print(
                f"systematic: {tuple_size}-way, {len(rolesets_list)} role-sets, "
                f"sample={sample_k or 'full'}, jobs={n_jobs}...",
                flush=True,
            )
            stats = await systematic_action_coverage(
                role_sets=rolesets_list,
                seed=int(args.seed) + tuple_size * 1_000_003,
                per_roleset_pair_samples=int(args.systematic_pair_samples),
                dedupe_actions=not bool(args.no_dedupe_actions),
                jobs=n_jobs,
                tuple_size=int(tuple_size),
                all_roles_act=not bool(args.isolated_systematic),
                sample_per_combo=int(sample_k),
            )
            print(
                f"systematic ({tuple_size}-way, sample={sample_k or 'full'}, "
                f"all_roles_act={not args.isolated_systematic}): "
                f"executed={stats.nights_executed} deduped_skips={stats.nights_deduped}",
                flush=True,
            )

    # Post-run invariants on a clean game.
    game, _guild, _members = make_game(seed=60, n=6)
    game.player_roles.update({1: "Townie", 2: "Townie", 3: "Townie", 4: "Townie", 5: "Townie", 6: "Townie"})
    assert_invariants(game)

    print(f"sim_test.py: OK (scenarios={n_scenarios})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

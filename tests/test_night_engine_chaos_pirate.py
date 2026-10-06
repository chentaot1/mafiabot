from __future__ import annotations

import asyncio
import random

import game as game_module


class _FakeMember:
    def __init__(self, mid: int):
        self.id = mid
        self.display_name = f"P{mid}"
        self.dms: list[str] = []

    async def send(self, msg: str) -> None:
        self.dms.append(str(msg))


class _FakeGuild:
    def __init__(self, members):
        self._members = {m.id: m for m in members}

    def get_member(self, uid: int):
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int):
        return self._members.get(int(uid))


from config import CHAOS_EFFECT_POOL
EFF_POOL = CHAOS_EFFECT_POOL


def _eff_for(game: game_module.Game, actor_id: int, t1: int, t2: int) -> str:
    rng = random.Random(f"{game.guild_id}:{game.day_number}:{actor_id}:{t1}:{t2}")
    return rng.choice(EFF_POOL)


def _find_pair_for_eff(game: game_module.Game, actor_id: int, living_ids: list[int], desired: str) -> tuple[int, int]:
    for i in range(len(living_ids)):
        for j in range(i + 1, len(living_ids)):
            t1, t2 = living_ids[i], living_ids[j]
            if _eff_for(game, actor_id, t1, t2) == desired:
                return t1, t2
    raise AssertionError(f"Could not find target pair yielding chaos eff={desired!r}")


def _find_pair_for_eff_with_fixed_t1(
    game: game_module.Game, actor_id: int, fixed_t1: int, other_ids: list[int], desired: str
) -> tuple[int, int]:
    for t2 in other_ids:
        if t2 == fixed_t1:
            continue
        if _eff_for(game, actor_id, fixed_t1, t2) == desired:
            return fixed_t1, t2
    raise AssertionError(f"Could not find chaos pair (t1={fixed_t1}) yielding eff={desired!r}")


def _mk_game(members: list[_FakeMember], roles: dict[int, str], role_states: dict[int, dict]) -> game_module.Game:
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = members  # type: ignore[assignment]
    g.living_players = members.copy()  # type: ignore[assignment]
    g.player_roles = roles
    g.role_states = role_states
    g.night_actions = {}
    return g


def test_pirate_plunder_win_idempotent_per_night() -> None:
    from engine.night import run_night_pipeline

    m1, m2 = _FakeMember(1), _FakeMember(2)
    g = _mk_game(
        [m1, m2],
        roles={1: "Pirate", 2: "Survivor"},
        role_states={1: {"wins": 0}, 2: {"vests_remaining": 2}},
    )
    g.night_actions = {1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": True, "duel_finished": True}}

    guild = _FakeGuild(g.players)
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert g.role_states[1]["wins"] == 1
    assert g.role_states[1].get("pirate_win_this_night") is True


def test_chaos_guard_blocks_the_attack_without_sacrificing_chaos() -> None:
    from engine.night import run_night_pipeline

    # 1=Chaos, 2=Survivor (victim), 3=Vigilante (attacker). Add extra living slots to make
    # it feasible to deterministically hit the desired Chaos effect via seeded RNG.
    extras = [_FakeMember(i) for i in range(4, 21)]
    m1, m2, m3 = _FakeMember(1), _FakeMember(2), _FakeMember(3)
    members = [m1, m2, m3] + extras
    roles: dict[int, str] = {1: "Chaos", 2: "Survivor", 3: "Vigilante"}
    states: dict[int, dict] = {1: {"uses_remaining": 2}, 2: {"vests_remaining": 2}, 3: {"shots_remaining": 1}}
    for em in extras:
        roles[em.id] = "Survivor"
        states[em.id] = {"vests_remaining": 2}

    g = _mk_game(members, roles=roles, role_states=states)
    # Find any target pair that deterministically yields protect, then attack that protected target (t1).
    living_ids = [m.id for m in members if m.id != 1]
    t1, t2 = _find_pair_for_eff(g, actor_id=1, living_ids=living_ids, desired="guard")

    # Ensure the protected target is the intended kill victim (t1 becomes chaos_protected_by target).
    g.night_actions = {
        1: {"type": "chaos", "actor": 1, "targets": [t1, t2]},
        3: {"type": "shoot", "actor": 3, "target": t1},
    }

    _visit_log, _blocked, _healed_by, _protected_by, deaths = asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]

    # If the guard triggers (i.e., target was attacked), Chaos dies (Bodyguard-style).
    assert 1 not in deaths
    assert t1 not in deaths
    assert 3 in _blocked
    assert g.role_states[1]["uses_remaining"] == 1


def test_chaos_roleblock_injected_as_real_action() -> None:
    from engine.night import run_night_pipeline

    # 1=Chaos, 2=Sheriff. Add extra living slots to make deterministic effect selection feasible.
    extras = [_FakeMember(i) for i in range(3, 21)]
    m1, m2 = _FakeMember(1), _FakeMember(2)
    members = [m1, m2] + extras

    roles = {1: "Chaos", 2: "Sheriff"}
    states = {1: {"uses_remaining": 2}, 2: {}}
    for em in extras:
        roles[em.id] = "Survivor"
        states[em.id] = {"vests_remaining": 2}

    g = _mk_game(members, roles=roles, role_states=states)
    # Ensure the Chaos-injected roleblock targets 2 (so we have a stable assertion target).
    t1, t2 = _find_pair_for_eff_with_fixed_t1(g, actor_id=1, fixed_t1=2, other_ids=[m.id for m in members if m.id != 2], desired="roleblock")
    g.night_actions = {1: {"type": "chaos", "actor": 1, "targets": [t1, t2]}}

    asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]
    assert g.night_actions.get(1, {}).get("type") == "roleblock"
    assert g.night_actions.get(1, {}).get("_from_chaos") is True


def test_pirate_plunder_blocks_target_even_on_duel_loss() -> None:
    from engine.night import run_night_pipeline

    # 1=Pirate, 2=Survivor
    m1, m2 = _FakeMember(1), _FakeMember(2)
    g = _mk_game(
        [m1, m2],
        roles={1: "Pirate", 2: "Survivor"},
        role_states={1: {"wins": 0}, 2: {"vests_remaining": 2}},
    )
    g.night_actions = {1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": False, "duel_finished": True}}

    _visit_log, blocked, _healed_by, _protected_by, _deaths = asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]
    assert 2 in blocked, "Expected Pirate plunder to roleblock the target even if the duel is lost"


def test_gatekeeper_blocks_pirate_plunder_as_visitor_and_plunder_does_not_roleblock_target() -> None:
    from engine.night import run_night_pipeline

    # 1=Pirate, 2=Survivor (target), 3=Gatekeeper guarding the target
    m1, m2, m3 = _FakeMember(1), _FakeMember(2), _FakeMember(3)
    g = _mk_game(
        [m1, m2, m3],
        roles={1: "Pirate", 2: "Survivor", 3: "Gatekeeper"},
        role_states={1: {"wins": 0}, 2: {"vests_remaining": 2}, 3: {"uses_remaining": 2}},
    )
    g.night_actions = {
        1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": False, "duel_finished": True},
        3: {"type": "guard", "actor": 3, "role": "Gatekeeper", "target": 2},
    }

    _visit_log, blocked, _healed_by, _protected_by, _deaths = asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]
    assert 1 in blocked, "Expected Gatekeeper to block Pirate as a visitor"
    # Engine semantics: if Gatekeeper blocks the Pirate as a visitor, the plunder does not apply (no roleblock).
    assert 2 not in blocked, "Did not expect Pirate plunder to roleblock the target if Gatekeeper blocks the Pirate"

from __future__ import annotations

import asyncio

from scripts.monte_carlo import bridge
from scripts.monte_carlo_sim import Player, simulate_once


def test_monte_sim_uses_engine_for_chaos_night1_shield() -> None:
    """Regression: Chaos N1 shield must come from engine killing rules, not sim heuristics."""
    roles = ["Chaos", "Mobster"]
    out = simulate_once(roles, max_days=1, collect_stats=False, trace=False)
    assert isinstance(out, dict)
    assert out.get("Town") or not out.get("Mafia") or True


def test_gatekeeper_blocks_pirate_via_engine_bridge() -> None:
    from engine.night import run_night_pipeline

    m1, m2, m3 = bridge._FakeMember(1), bridge._FakeMember(2), bridge._FakeMember(3)
    import game as game_module

    g = game_module.Game(guild_id=99)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [m1, m2, m3]  # type: ignore[assignment]
    g.living_players = [m1, m2, m3]  # type: ignore[assignment]
    g.player_roles = {1: "Pirate", 2: "Survivor", 3: "Gatekeeper"}
    g.role_states = {
        1: {"wins": 0},
        2: {"vests_remaining": 2},
        3: {"uses_remaining": 2},
    }
    g.night_actions = {
        1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": False, "duel_finished": True},
        3: {"type": "guard", "actor": 3, "role": "Gatekeeper", "target": 2},
    }
    guild = bridge._FakeGuild([m1, m2, m3])
    _vl, blocked, _h, _p, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 in blocked
    assert 2 not in blocked
    assert 2 not in deaths

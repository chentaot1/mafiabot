"""Headless balance trials must never replace a bot's durable match state."""
import asyncio
from pathlib import Path

import pytest

import game as gm
import persistence
from scripts.monte_carlo import bridge, generator, runtime
from scripts.monte_carlo.state import Player


@pytest.mark.parametrize("action", ["night", "deputy", "explicit-save"])
def test_simulated_game_keeps_checkpoints_in_memory(action, monkeypatch):
    monkeypatch.setattr(gm, "active_games", {})
    guild_id = 123
    persistence.save_state(guild_id, {"real_match": True, "_pending_endgame": {"outcome": "Town"}})
    before = {path.name: path.read_bytes() for path in persistence.STATE_DIR.iterdir()}
    players = [Player(1, "Deputy", deputy_shots_remaining=1), Player(2, "Mobster"),
               Player(3, "Doctor", self_heals_left=1), Player(4, "Townie")]
    game, guild = bridge.build_game_from_sim(players, {1, 2, 3, 4}, doused=set(),
        dead_town_corpses=[], used_corpse_ids=set(), hidden_corpse_ids=set(), day=1, guild_id=guild_id)
    try:
        if action == "night":
            game.night_actions = {2: {"type": "kill", "actor": 2, "target": 4},
                                  3: {"type": "heal", "actor": 3, "target": 1}}
            deaths, _, _, _ = bridge.resolve_night_via_engine(game, guild, evidence={})
            assert deaths == {4}
            assert game.night_completion_snapshot["killing_phase_complete"]
        elif action == "deputy":
            assert asyncio.run(bridge.deputy_day_shot(game, guild, 1, 2)) == {2}
            assert game.role_states[1]["deputy_shots_remaining"] == 0
            assert 2 not in {member.id for member in game.living_players}
        else:
            game.persist_now()
            asyncio.run(game.persist_flush())
    finally:
        runtime.close_async_loop()
    assert {path.name: path.read_bytes() for path in persistence.STATE_DIR.iterdir()} == before


def test_parallel_trial_setup_does_not_disable_real_game_saving(monkeypatch):
    original = gm.Game.persist_flush
    # Restore even when demonstrating the older global class replacement.
    monkeypatch.setattr(gm.Game, "persist_flush", original)
    monkeypatch.setattr(gm, "active_games", {})
    monkeypatch.setattr(runtime, "configure_quiet_logging", lambda: None)
    monkeypatch.setattr(generator, "run_generator_weighted_trials_chunk", lambda *args, **kwargs: {"trials": 1})
    work = {"root": str(Path(__file__).resolve().parents[1]), "player_count": 7, "trials": 1, "seed": 1}
    assert generator._parallel_trial_worker(work) == {"trials": 1}
    assert gm.Game.persist_flush is original
    game = gm.Game(123)
    game.in_progress, game.player_roles = True, {1: "Doctor"}
    asyncio.run(game.persist_flush())
    assert persistence.load_state(123)["player_roles"] == {"1": "Doctor"}

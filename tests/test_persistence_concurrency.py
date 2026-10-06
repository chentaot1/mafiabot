import asyncio
import threading

import pytest

import persistence
from game import Game


@pytest.mark.parametrize("cancel_first", [False, True])
def test_saves_stay_ordered_even_if_first_caller_is_cancelled(tmp_path, monkeypatch, cancel_first):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    original = persistence.save_state

    def delayed_save(guild_id, data):
        if data["version"] == 1:
            first_started.set()
            assert release_first.wait(5), "first writer was not released"
        else:
            second_started.set()
        original(guild_id, data)

    monkeypatch.setattr(persistence, "save_state", delayed_save)

    async def run():
        first = asyncio.create_task(persistence.save_state_async(123, {"version": 1, "actions": {"1": "heal"}}))
        assert await asyncio.to_thread(first_started.wait, 5)
        if cancel_first:
            first.cancel()
        second = asyncio.create_task(persistence.save_state_async(123, {"version": 2, "actions": {"1": "heal", "2": "kill"}}))
        try:
            # Give a wrongly concurrent second worker time to overtake the first.
            assert not await asyncio.to_thread(second_started.wait, 0.1)
        finally:
            release_first.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        if cancel_first:
            assert isinstance(outcomes[0], asyncio.CancelledError)
        else:
            assert outcomes[0] is None
        assert outcomes[1] is None

    asyncio.run(run())
    assert persistence.load_state(123) == {"version": 2, "actions": {"1": "heal", "2": "kill"}}


def test_game_snapshot_is_detached_from_later_mutations():
    game = Game(123)
    game.night_actions = {1: {"type": "transport", "targets": [2, 3]}}
    game.role_states = {1: {"uses_remaining": 2}}
    game.graveyard = [{"player_id": 4, "used_by_retri": False}]
    snapshot = game.to_persisted()
    game.night_actions[1]["targets"][0] = 5
    game.role_states[1]["uses_remaining"] = 1
    game.graveyard[0]["used_by_retri"] = True
    assert snapshot["night_actions"]["1"]["targets"] == [2, 3]
    assert snapshot["role_states"]["1"]["uses_remaining"] == 2
    assert snapshot["graveyard"][0]["used_by_retri"] is False


def test_failed_save_preserves_prior_snapshot_and_allows_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    persistence.save_state(123, {"version": 1})

    async def run():
        with pytest.raises(TypeError):
            await persistence.save_state_async(123, {"bad": object()})
        assert persistence.load_state(123) == {"version": 1}
        await persistence.save_state_async(123, {"version": 2})

    asyncio.run(run())
    assert persistence.load_state(123) == {"version": 2}
    assert not (tmp_path / "123.json.tmp").exists()

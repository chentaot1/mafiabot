"""Shutdown drains private control deliveries and their recovery checkpoints."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import game as gm
import persistence
from game import Game
from gameplay import actions, duels
from gameplay.lifecycle import message_lock
from test_gameplay import world


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["panel", "duel_prompt", "duel_result_send", "duel_result_edit"])
@pytest.mark.parametrize("stage", ["request", "checkpoint"])
async def test_cancelled_delivery_saves_reference_before_releasing_match_boundary(world, monkeypatch, kind, stage):
    game, guild, controller, channels, _ = world
    destination = channels[101]
    token = None
    if kind != "panel":
        game.player_roles[1] = "Pirate"
        result = await actions.submit(game, 1, "plunder", (3,))
        token = result.action["duel_token"]
        if kind == "duel_result_edit":
            await controller.duel_prompt(game, 1, 1, result.action)
        if kind.startswith("duel_result"):
            await duels.choose(game, 1, token, 1, "rock")
            await duels.choose(game, 1, token, 3, "scissors")
            await duels.complete(game, 1, token)

    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "checkpoint":
        original = game.persist_flush
        target, attribute = game, "persist_flush"
    elif kind == "duel_result_edit":
        message = next(iter(destination.messages.values()))
        original = message.edit
        target, attribute = message, "edit"
    else:
        original = destination.send
        target, attribute = destination, "send"
    async def paused_request(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(target, attribute, paused_request)
    registered = []
    monkeypatch.setattr(controller.bot, "add_view", lambda view, **kwargs: registered.append(kwargs["message_id"]))

    def deliver(current):
        if kind == "panel":
            return controller.send_panel(current, 1)
        return controller.duel_prompt(current, 1, 1, duels.get_duel(current, 1, token, open_only=False))
    task = controller.start_job((game.guild_id, "shutdown-test", kind), lambda: deliver(game))
    await asyncio.wait_for(entered.wait(), 2)
    shutdown = asyncio.create_task(controller.stop_game(game))
    try:
        await asyncio.sleep(0)
        assert message_lock(game.guild_id).locked()
        assert not shutdown.done()
        # A second cancellation must not interrupt the checkpoint either.
        task.cancel()
        release.set()
        await asyncio.wait_for(shutdown, 3)
    finally:
        release.set()
        await controller.stop_game(game)
    assert task.cancelled()
    assert not message_lock(game.guild_id).locked()
    saved = persistence.load_state(game.guild_id)
    if kind == "panel":
        reference = saved["gameplay"]["panels"]["1"]
    else:
        record = saved["night_actions"]["1"]
        reference = record["duel_prompts"]["1"]
        if kind.startswith("duel_result"):
            assert record["duel_delivered"] == [1]
            assert saved["gameplay"]["duels"][token]["duel_delivered"] == [1]
    assert reference == {"channel_id": 101, "message_id": 1}
    assert registered == [1]

    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    gm.active_games[game.guild_id] = recovered
    recovered.check_win_conditions = AsyncMock(return_value=False)
    await deliver(recovered)
    assert len(destination.sent) == 1
    assert registered == [1, 1]

"""Completed duel delivery retries preserve the committed outcome and receipts."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import persistence
from gameplay import actions, duels
from test_gameplay import world


async def completed_duel(game):
    game.player_roles[1] = "Pirate"
    result = await actions.submit(game, 1, "plunder", (3,))
    token = result.action["duel_token"]
    await duels.choose(game, 1, token, 1, "rock")
    await duels.choose(game, 1, token, 3, "scissors")
    await duels.complete(game, 1, token)
    return token


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_uid", [1, 3])
@pytest.mark.parametrize("archived", [False, True])
async def test_transient_result_failure_retries_only_undelivered_recipient(world, monkeypatch, failed_uid, archived):
    game, _, controller, channels, _ = world
    token = await completed_duel(game)
    committed = deepcopy(game.gameplay["duels"][token])
    if archived:
        game.night_actions.clear()
        game.phase, game.day_number = "day", 2
        await game.persist_flush()
    old_phase = game.phase, game.day_number
    monkeypatch.setattr(duels, "complete", AsyncMock(side_effect=AssertionError("committed duel was rerolled")))
    destination = channels[100 + failed_uid]
    original_send = destination.send
    attempts = 0
    async def send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise discord.HTTPException(S(status=503, reason="temporarily unavailable"), "test failure")
        return await original_send(*args, **kwargs)
    monkeypatch.setattr(destination, "send", send)

    task = controller.start_job((game.guild_id, "duel", token), lambda: controller.run_duel(game, 1, token))
    try:
        await asyncio.wait_for(task, 3)
    finally:
        await controller.stop_game(game)
    record = game.gameplay["duels"][token]
    assert set(record["duel_delivered"]) == {1, 3}
    assert attempts == 2
    for uid in (1, 3):
        assert len(channels[100 + uid].sent) == 1
    for key in ("duel_choices", "duel_won", "duel_result", "duel_deadline", "duel_finished"):
        assert record[key] == committed[key]
    assert (game.phase, game.day_number) == old_phase
    saved = persistence.load_state(game.guild_id)["gameplay"]["duels"][token]
    assert set(saved["duel_delivered"]) == {1, 3}
    assert saved["duel_won"] is True


@pytest.mark.asyncio
async def test_blocked_recipient_does_not_stop_delivery_to_other_player(world, monkeypatch):
    game, _, controller, channels, players = world
    token = await completed_duel(game)
    blocked = discord.Forbidden(S(status=403, reason="blocked"), "test failure")
    monkeypatch.setattr(players[2], "create_dm", AsyncMock(side_effect=blocked))
    task = controller.start_job((game.guild_id, "duel", token), lambda: controller.run_duel(game, 1, token))
    try:
        await asyncio.wait_for(task, 2)
    finally:
        await controller.stop_game(game)
    record = game.gameplay["duels"][token]
    assert record["duel_finished"] and record["duel_won"]
    assert record["duel_delivered"] == [1]
    assert len(channels[101].sent) == 1
    assert not channels[103].sent

"""Shutdown saves successful public announcements even without message history."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import game as gm
import persistence
from game import Game
from gameplay import state as st
from gameplay.death import apply_death
from gameplay.lifecycle import message_lock
from test_gameplay import world


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["absent", "forbidden"])
@pytest.mark.parametrize("stage", ["request", "checkpoint"])
async def test_shutdown_saves_death_announcement_before_unlock_and_restart(world, monkeypatch, history, stage):
    game, guild, controller, channels, players = world
    await st.commit(game, lambda: apply_death(game, 1, "manual"))
    receipt = game.gameplay["deaths"]["1"]
    destination = channels[10]
    if history == "forbidden":
        monkeypatch.setattr(destination, "history", Mock(side_effect=discord.Forbidden(
            S(status=403, reason="history unavailable"), "offline test")), raising=False)
    players[0].roles = [guild.get_role(game.alive_role_id)]
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "request":
        original_send = destination.send
        async def send(*args, **kwargs):
            message = await original_send(*args, **kwargs)
            entered.set()
            await release.wait()
            return message
        monkeypatch.setattr(destination, "send", send)
    else:
        original_flush = game.persist_flush
        async def persist():
            if receipt.get("announcement_id"):
                entered.set()
                await release.wait()
            await original_flush()
        monkeypatch.setattr(game, "persist_flush", persist)
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    task = controller.start_job((game.guild_id, "death", 1),
        lambda: game.deliver_death_receipt(destination, guild, receipt))
    await asyncio.wait_for(entered.wait(), 2)
    shutdown = asyncio.create_task(controller.stop_game(game))
    try:
        await asyncio.sleep(0)
        assert not shutdown.done() and message_lock(game.guild_id).locked()
        task.cancel()
        release.set()
        await asyncio.wait_for(shutdown, 3)
    finally:
        release.set()
        await controller.stop_game(game)
    assert task.cancelled() and not message_lock(game.guild_id).locked()
    saved = persistence.load_state(game.guild_id)
    assert saved["gameplay"]["deaths"]["1"]["announcement_id"] == 1
    assert len(destination.sent) == 1
    players[0].remove_roles.assert_awaited_once()
    if history == "forbidden":
        destination.history.assert_called_once_with(limit=100)

    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    gm.active_games[game.guild_id] = recovered
    recovered.check_win_conditions = AsyncMock(return_value=False)
    for _ in range(2):
        await recovered.deliver_death_receipt(destination, guild, recovered.gameplay["deaths"]["1"])
    assert len(destination.sent) == 1
    assert len(recovered.graveyard) == 1
    assert recovered.gameplay["deaths"]["1"]["delivered"]
    players[0].remove_roles.assert_awaited_once()
    if history == "forbidden":
        destination.history.assert_called_once_with(limit=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 503])
async def test_public_send_failure_keeps_announcement_pending_without_repeating_cleanup(world, monkeypatch, status):
    game, guild, controller, channels, players = world
    await st.commit(game, lambda: apply_death(game, 1, "manual"))
    receipt = game.gameplay["deaths"]["1"]
    players[0].roles = [guild.get_role(game.alive_role_id)]
    original_send = channels[10].send
    error_type = discord.Forbidden if status == 403 else discord.HTTPException
    monkeypatch.setattr(channels[10], "send", AsyncMock(side_effect=error_type(
        S(status=status, reason="offline test"), "public delivery failed")))
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    await game.deliver_death_receipt(channels[10], guild, receipt)
    assert receipt["access_cleaned"] and receipt["notices_delivered"]
    assert not receipt["announcement_id"] and not receipt["delivered"]
    assert not channels[10].sent
    monkeypatch.setattr(channels[10], "send", original_send)
    await game.deliver_death_receipt(channels[10], guild, receipt)
    assert receipt["delivered"] and len(channels[10].sent) == 1
    players[0].remove_roles.assert_awaited_once()

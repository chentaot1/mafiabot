"""Night feedback survives interrupted delivery without replaying saved results."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from game import Game
from gameplay import reports, resolution, state as st
from gameplay.lifecycle import message_lock
from gameplay.views import ReportCard
from test_gameplay import world


async def prepared_feedback(world, rich):
    game, guild, controller, channels, players = world
    players[0]._feedback_send = players[0].send
    game.resolving = True
    text = "Your saved investigation result."
    if rich:
        game.player_roles[1] = "Seer"
        reports.remember(game, 1, text, kind="seer", selected=(2, 3))
    game.gameplay["resolution"] = {
        "night_token": "night-one", "day": 1, "applied": True, "progressed": False,
        "death_ids": [], "public_delivery_pending": False, "feedback_index": 0,
        "feedback": [{"user_id": 1, "text": text}, {"user_id": 2, "text": "Second saved result."}],
    }
    game.gameplay.setdefault("resolutions", {})["night-one"] = game.gameplay["resolution"]
    await game.persist_flush()
    return S(guild=guild, send=channels[10].send)


def first_deliveries(world, rich):
    _, _, _, channels, players = world
    if rich:
        return sum(isinstance(kwargs.get("view"), ReportCard) for _, kwargs in channels[101].sent)
    return players[0]._feedback_send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("stage", ["request", "checkpoint"])
async def test_shutdown_finishes_feedback_and_receipt_then_recovery_advances_once(world, monkeypatch, rich, stage):
    game, guild, controller, channels, players = world
    ctx = await prepared_feedback(world, rich)
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "checkpoint":
        target, attribute = game, "persist_flush"
    elif rich:
        target, attribute = channels[101], "send"
    else:
        target, attribute = players[0], "send"
    original = getattr(target, attribute)
    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(target, attribute, paused)
    task = controller.start_job((game.guild_id, "resolution", "night-one"), lambda: resolution.finish(game, ctx))
    await asyncio.wait_for(entered.wait(), 2)
    shutdown = asyncio.create_task(controller.stop_game(game))
    try:
        await asyncio.sleep(0)
        assert not shutdown.done()
        boundary_locked = message_lock(game.guild_id).locked()
        task.cancel()
        release.set()
        await asyncio.wait_for(shutdown, 3)
    finally:
        release.set()
        await controller.stop_game(game)
    assert task.cancelled()
    assert first_deliveries(world, rich) == 1
    saved = persistence.load_state(game.guild_id)
    assert saved["gameplay"]["resolution"]["feedback_index"] == 1
    assert boundary_locked
    assert saved["phase"] == "night" and saved["day_number"] == 1
    assert players[1].send.await_count == 0
    assert not message_lock(game.guild_id).locked()

    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    recovered.check_win_conditions = AsyncMock(return_value=False)
    gm.active_games[game.guild_id] = recovered
    await resolution.finish(recovered, ctx)
    await resolution.finish(recovered, ctx)
    assert first_deliveries(world, rich) == 1
    assert players[1].send.await_count == 1
    assert recovered.phase == "day" and recovered.day_number == 2
    assert not recovered.resolving
    saved = persistence.load_state(game.guild_id)
    assert saved["gameplay"]["resolution"]["feedback_index"] == 2
    assert saved["gameplay"]["resolution"]["progressed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rich,lookup", [(False, "member"), (True, "member"), (True, "destination")])
@pytest.mark.parametrize("invalidation", ["ending", "replaced"])
async def test_feedback_does_not_send_when_match_changes_during_lookup(world, monkeypatch, rich, lookup, invalidation):
    game, _, controller, _, _ = world
    ctx = await prepared_feedback(world, rich)
    entered, release = asyncio.Event(), asyncio.Event()
    target, attribute = (game, "get_member_safe") if lookup == "member" else (controller, "private_destination")
    original = getattr(target, attribute)
    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(target, attribute, paused)
    task = asyncio.create_task(resolution.finish(game, ctx))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if invalidation == "ending":
            game.ending = True
        else:
            gm.active_games[game.guild_id] = Game(game.guild_id)
        release.set()
        with pytest.raises(st.Rejected):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert first_deliveries(world, rich) == 0
    assert persistence.load_state(game.guild_id)["gameplay"]["resolution"]["feedback_index"] == 0
    assert not message_lock(game.guild_id).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("status", [403, 503])
async def test_feedback_delivery_distinguishes_blocked_dm_from_retryable_failure(world, monkeypatch, rich, status):
    game, _, _, channels, players = world
    ctx = await prepared_feedback(world, rich)
    target, attribute = (channels[101], "send") if rich else (players[0], "send")
    original = getattr(target, attribute)
    error_type = discord.Forbidden if status == 403 else discord.HTTPException
    attempts = 0
    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type(S(status=status, reason="test failure"), "offline test")
        return await original(*args, **kwargs)
    monkeypatch.setattr(target, attribute, fail_once)
    if status == 503:
        with pytest.raises(discord.HTTPException):
            await resolution.finish(game, ctx)
        assert persistence.load_state(game.guild_id)["gameplay"]["resolution"]["feedback_index"] == 0
        assert game.phase == "night" and players[1].send.await_count == 0
    await resolution.finish(game, ctx)
    assert game.phase == "day" and game.day_number == 2
    assert game.gameplay["resolution"]["feedback_index"] == 2
    assert first_deliveries(world, rich) == (1 if status == 503 else 0)
    assert players[1].send.await_count == 1

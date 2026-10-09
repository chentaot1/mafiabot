"""Startup posts save their acknowledgement before graceful shutdown returns."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from game import Game
from gameplay import startup, state as st
from gameplay.lifecycle import message_lock
from test_gameplay import world


def ready(world):
    game, guild, controller, channels, _ = world
    game.phase, game.mafia_tc_id = "day", 11
    game.gameplay["startup"] = {"match": game.game_key, "complete": True,
        "announced": False, "mafia_announced": False, "completed_players": [1, 2, 3, 4]}
    persistence.save_state(game.guild_id, game.to_persisted())
    return game, guild, controller, channels


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["mafia", "public"])
@pytest.mark.parametrize("stage", ["request", "checkpoint"])
async def test_shutdown_saves_startup_post_and_restart_sends_only_missing_post(world, monkeypatch, destination, stage):
    game, guild, controller, channels = ready(world)
    flag = "mafia_announced" if destination == "mafia" else "announced"
    channel = channels[11 if destination == "mafia" else 10]
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "request":
        original_send = channel.send
        async def send(*args, **kwargs):
            message = await original_send(*args, **kwargs)
            entered.set()
            await release.wait()
            return message
        monkeypatch.setattr(channel, "send", send)
    else:
        original_flush = game.persist_flush
        async def persist():
            if game.gameplay["startup"].get(flag):
                entered.set()
                await release.wait()
            await original_flush()
        monkeypatch.setattr(game, "persist_flush", persist)
    task = controller.start_job((game.guild_id, "startup", game.game_key),
        lambda: startup.resume(game, guild, client=S(gameplay_controller=None)))
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
    saved = persistence.load_state(game.guild_id)
    assert saved["gameplay"]["startup"][flag]
    assert len(channel.sent) == 1
    if destination == "mafia":
        assert not saved["gameplay"]["startup"]["announced"]
        assert not channels[10].sent
    assert task.cancelled() and not message_lock(game.guild_id).locked()
    assert boundary_locked
    assert not game._startup_lock.locked()

    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    gm.active_games[game.guild_id] = recovered
    for _ in range(2):
        await startup.resume(recovered, guild, client=S(gameplay_controller=None))
    assert recovered.gameplay["startup"]["announced"]
    assert len(channels[10].sent) == len(channels[11].sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["record", "model", "match", "replacement", "ended"])
async def test_startup_post_waiting_for_delivery_lock_rechecks_match(world, change):
    game, guild, _, channels = ready(world)
    async with message_lock(game.guild_id):
        task = asyncio.create_task(startup.announce(game, guild))
        await asyncio.sleep(0)
        if change == "record":
            game.gameplay["startup"]["match"] = "earlier-match"
        elif change == "model":
            gm.active_games[game.guild_id] = Game(game.guild_id)
        elif change == "match":
            game.game_key = "later-match"
        elif change == "replacement":
            game.game_key = "later-match"
            game.gameplay["startup"]["match"] = game.game_key
        else:
            game.in_progress = False
    with pytest.raises(st.Rejected):
        await task
    assert not channels[10].sent and not channels[11].sent


@pytest.mark.asyncio
async def test_public_failure_retries_without_repeating_mafia_welcome(world, monkeypatch):
    game, guild, _, channels = ready(world)
    original_send = channels[10].send
    monkeypatch.setattr(channels[10], "send", AsyncMock(side_effect=discord.HTTPException(
        S(status=503, reason="unavailable"), "offline failure")))
    with pytest.raises(discord.HTTPException):
        await startup.announce(game, guild)
    assert persistence.load_state(game.guild_id)["gameplay"]["startup"]["mafia_announced"]
    assert not game.gameplay["startup"]["announced"]
    monkeypatch.setattr(channels[10], "send", original_send)
    await startup.announce(game, guild)
    assert len(channels[10].sent) == len(channels[11].sent) == 1


@pytest.mark.asyncio
async def test_unavailable_guild_keeps_startup_pending_for_retry(world):
    game, guild, _, channels = ready(world)
    with pytest.raises(OSError):
        await startup.announce(game, None)
    assert not game.gameplay["startup"]["mafia_announced"]
    assert not game.gameplay["startup"]["announced"]
    await startup.announce(game, guild)
    assert len(channels[10].sent) == len(channels[11].sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('phase,day', [('night', 1), ('day', 2), ('night', 2)])
async def test_delayed_startup_post_reports_current_phase_after_play_advances(world, monkeypatch, phase, day, restart):
    game, guild, _, channels = ready(world)
    monkeypatch.setattr(gm, '_BOT', S())
    original_send = channels[10].send
    channels[10].send = AsyncMock(side_effect=discord.HTTPException(
        S(status=503, reason='temporary failure'), 'retry'))
    with pytest.raises(discord.HTTPException):
        await startup.announce(game, guild)
    ctx = S(guild=guild, send=AsyncMock())
    await game.start_night(ctx)
    if day == 2:
        await game.start_day(ctx)
        if phase == 'night':
            await game.start_night(ctx)
    assert (game.phase, game.day_number) == (phase, day)
    if restart:
        game = Game.from_persisted(persistence.load_state(game.guild_id))
        await game.rehydrate_members(guild)
        gm.active_games[game.guild_id] = game
    channels[10].send = original_send
    await startup.announce(game, guild)
    await startup.announce(game, guild)
    assert len(channels[10].sent) == len(channels[11].sent) == 1
    text = channels[10].sent[0][0]
    assert f'**{phase.title()} {day}**' in text
    assert '**Day 1**' not in text
    assert game.gameplay['startup']['announced']


@pytest.mark.asyncio
async def test_startup_post_reads_phase_after_mafia_welcome_finishes(world, monkeypatch):
    game, guild, _, channels = ready(world)
    monkeypatch.setattr(gm, '_BOT', S())
    original_send = channels[11].send
    async def welcome(*args, **kwargs):
        await game.start_night(S(guild=guild, send=AsyncMock()))
        return await original_send(*args, **kwargs)
    monkeypatch.setattr(channels[11], 'send', welcome)
    await startup.announce(game, guild)
    assert '**Night 1**' in channels[10].sent[0][0]


@pytest.mark.asyncio
async def test_legacy_unknown_phase_does_not_invent_day_one(world):
    game, guild, _, channels = ready(world)
    game.phase = None
    await startup.announce(game, guild)
    assert 'Roles have been assigned secretly.' in channels[10].sent[0][0]
    assert '**Day 1**' not in channels[10].sent[0][0]

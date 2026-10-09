"""Resolution cancellation releases only owned, unapplied work."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

import persistence
from gameplay import actions, resolution, state as st
from test_gameplay import world


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["entry", "application"])
async def test_cancelled_checkpoint_allows_safe_retry_without_reopening_applied_result(world, monkeypatch, stage):
    game, guild, _, channels, _ = world
    original_flush = game.persist_flush
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    pause_at = 1 if stage == "entry" else 2
    async def persist():
        nonlocal calls
        calls += 1
        if calls == pause_at:
            entered.set()
            await release.wait()
        await original_flush()
    monkeypatch.setattr(game, "persist_flush", persist)
    async def calculate(working, server):
        working.role_states[1]["self_heals_remaining"] = 0
        return set(), [], []
    evaluate = AsyncMock(side_effect=calculate)
    monkeypatch.setattr(resolution, "evaluate", evaluate)
    ctx = S(guild=guild, send=channels[10].send)
    task = asyncio.create_task(resolution.run(game, ctx))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not game.state_lock.locked()
    saved = persistence.load_state(game.guild_id)
    if stage == "entry":
        assert not game.resolving and not saved["resolving"]
        assert not saved["gameplay"].get("resolution")
        assert evaluate.await_count == 0
        assert game.role_states[1]["self_heals_remaining"] == 1
        await resolution.run(game, ctx)
    else:
        assert game.resolving and saved["resolving"]
        assert saved["gameplay"]["resolution"]["applied"]
        assert not (await actions.submit(game, 1, "heal", (1,))).accepted
        await resolution.finish(game, ctx)
    assert evaluate.await_count == 1
    assert game.phase == "day" and game.day_number == 2 and not game.resolving
    assert game.role_states[1]["self_heals_remaining"] == 0
    assert game.gameplay["resolution"]["progressed"]


@pytest.mark.asyncio
async def test_rejected_second_resolution_does_not_release_active_owner(world, monkeypatch):
    game, guild, _, channels, _ = world
    entered, release = asyncio.Event(), asyncio.Event()
    async def calculate(*args):
        entered.set()
        await release.wait()
        return set(), [], []
    monkeypatch.setattr(resolution, "evaluate", calculate)
    ctx = S(guild=guild, send=channels[10].send)
    task = asyncio.create_task(resolution.run(game, ctx))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(st.Rejected, match="already in progress"):
            await resolution.run(game, ctx)
        assert game.resolving
        release.set()
        await asyncio.wait_for(task, 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert game.phase == "day" and game.day_number == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_another_resolution(world, monkeypatch):
    game, guild, _, channels, _ = world
    game.resolving = True
    synced = asyncio.Event()
    async def sync(*args):
        synced.set()
    monkeypatch.setattr(game, "sync_living_players", sync)
    async with game.state_lock:
        task = asyncio.create_task(resolution.run(game, S(guild=guild, send=channels[10].send)))
        await asyncio.wait_for(synced.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert game.resolving

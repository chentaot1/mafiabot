"""Death notices retry independently from committed death effects and announcements."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from game import Game
from gameplay import state as st
from gameplay.death import apply_death
from gameplay.records import load_ui
from test_gameplay import world


async def prepare(world, kind):
    game, _, _, _, _ = world
    if kind == "conversion":
        # Two independent Executioners convert from the same target's death.
        game.player_roles[2] = "Executioner"
        game.role_states[2] = {"exe_target": 3}
    await st.commit(game, lambda: apply_death(game, 3, "lynch" if kind == "haunt" else "night_kill", voters=(1, 2)))
    return game.gameplay["deaths"]["3"]


async def reload_game(world):
    game, guild, _, _, _ = world
    recovered = Game.from_persisted(persistence.load_state(game.guild_id))
    await recovered.rehydrate_members(guild)
    recovered.check_win_conditions = AsyncMock(return_value=False)
    gm.active_games[game.guild_id] = recovered
    return recovered


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["haunt", "conversion"])
async def test_transient_death_notice_failure_retries_after_restart_without_repeating_other_deliveries(world, monkeypatch, kind):
    game, guild, controller, channels, players = world
    receipt = await prepare(world, kind)
    recipient = players[2] if kind == "haunt" else players[1]
    original = recipient.send
    attempts = 0
    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise discord.HTTPException(S(status=503, reason="offline test"), "temporary failure")
        return await original(*args, **kwargs)
    monkeypatch.setattr(recipient, "send", fail_once)
    # Inspect the durable pending record before its supervised retry executes.
    retry_death = controller.retry_death
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    await game.deliver_death_receipt(channels[10], guild, receipt)
    assert receipt["access_cleaned"] and receipt["announcement_id"]
    assert not receipt["notices_delivered"] and not receipt["delivered"]
    if kind == "conversion":
        players[3].send.assert_awaited_once()
        assert receipt["notice_delivered_ids"] == [4]
    assert len(channels[10].sent) == 1
    recovered = await reload_game(world)
    retry_death(recovered, 3)
    task = controller.jobs[(game.guild_id, "death-delivery", game.game_key, 3)]
    try:
        await asyncio.wait_for(task, 3)
    finally:
        await controller.stop_game(recovered)
    await recovered.deliver_death_receipt(channels[10], guild, recovered.gameplay["deaths"]["3"])
    saved = persistence.load_state(game.guild_id)["gameplay"]["deaths"]["3"]
    assert saved["delivered"] and saved["notices_delivered"]
    assert attempts == 2 and original.await_count == 1
    assert len(channels[10].sent) == 1 and len(recovered.graveyard) == 1
    if kind == "conversion":
        assert set(saved["notice_delivered_ids"]) == {2, 4}
        players[3].send.assert_awaited_once()
        assert recovered.player_roles[2] == recovered.player_roles[4] == "Jester"
    else:
        assert saved["notice_delivered_ids"] == [3]
        assert recovered.role_states[3]["jester_won"] and recovered.role_states[3]["can_haunt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["haunt", "conversion"])
async def test_blocked_death_notice_is_terminal_and_other_recipients_still_receive_notice(world, monkeypatch, kind):
    game, guild, controller, channels, players = world
    receipt = await prepare(world, kind)
    recipient = players[2] if kind == "haunt" else players[1]
    recipient.send = AsyncMock(side_effect=discord.Forbidden(S(status=403, reason="blocked"), "offline test"))
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    await game.deliver_death_receipt(channels[10], guild, receipt)
    assert receipt["notices_delivered"] and receipt["delivered"]
    if kind == "conversion":
        players[3].send.assert_awaited_once()
    recovered = await reload_game(world)
    await recovered.deliver_death_receipt(channels[10], guild, recovered.gameplay["deaths"]["3"])
    recipient.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed,expected", [
    (None, []), ({"2": True}, []), ("2,4", []),
    ([True, 3, 4, 4, "2", 2, 999], [4, 2]), ([2, 2, 4], [2, 4]),
])
async def test_recovery_validates_notice_receipts_against_actual_recipients(world, completed, expected):
    game, _, _, _, _ = world
    receipt = await prepare(world, "conversion")
    receipt["notice_delivered_ids"] = completed
    saved = load_ui(game.gameplay)
    assert saved["deaths"]["3"]["notice_delivered_ids"] == expected


@pytest.mark.asyncio
async def test_legacy_completed_notices_are_not_resent_when_public_delivery_resumes(world, monkeypatch):
    game, guild, controller, channels, players = world
    receipt = await prepare(world, "conversion")
    receipt.pop("notice_delivered_ids", None)
    receipt["notices_delivered"] = True
    await game.persist_flush()
    recovered = await reload_game(world)
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    await recovered.deliver_death_receipt(channels[10], guild, recovered.gameplay["deaths"]["3"])
    assert recovered.gameplay["deaths"]["3"]["delivered"]
    assert len(channels[10].sent) == 1
    players[1].send.assert_not_awaited()
    players[3].send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["haunt", "conversion"])
async def test_shutdown_finishes_death_notice_receipt_before_restart(world, monkeypatch, kind):
    game, guild, controller, channels, players = world
    receipt = await prepare(world, kind)
    recipient = players[2] if kind == "haunt" else players[1]
    original = recipient.send
    entered, release = asyncio.Event(), asyncio.Event()
    async def paused_send(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await release.wait()
    monkeypatch.setattr(recipient, "send", paused_send)
    monkeypatch.setattr(controller, "retry_death", lambda *args: None)
    task = asyncio.create_task(game.deliver_death_receipt(channels[10], guild, receipt))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    saved = persistence.load_state(game.guild_id)["gameplay"]["deaths"]["3"]
    assert recipient.id in saved["notice_delivered_ids"]
    recovered = await reload_game(world)
    await recovered.deliver_death_receipt(channels[10], guild, recovered.gameplay["deaths"]["3"])
    assert original.await_count == 1
    assert len(channels[10].sent) == 1
    if kind == "conversion":
        players[3].send.assert_awaited_once()

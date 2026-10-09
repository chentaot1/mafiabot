"""Automatic controls retry temporary failures only within their original phase."""
import asyncio
from types import SimpleNamespace as S

import discord
import pytest

import persistence
from gameplay import state as st
from gameplay.views import DayPanel, NightPanel
from test_gameplay import world


def setup_phase(world, phase):
    game, _, controller, channels, _ = world
    if phase == "day":
        game.phase, game.day_number = "day", 2
        game.player_roles[1] = "Deputy"
        game.role_states[1] = {"deputy_shots_remaining": 1}
    return game, controller, channels[101]


async def send_panels(controller, game, phase):
    await (controller.send_day_panels(game) if phase == "day" else controller.send_night_panels(game))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["night", "day"])
async def test_automatic_panel_retries_and_saves_one_reusable_message(world, monkeypatch, phase):
    game, controller, destination = setup_phase(world, phase)
    expected = st.identity(game)
    original = destination.send
    attempts = 0
    async def fail_twice(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise discord.HTTPException(S(status=503, reason="offline test"), "temporary failure")
        return await original(*args, **kwargs)
    monkeypatch.setattr(destination, "send", fail_twice)
    await send_panels(controller, game, phase)
    key = (game.guild_id, "panel", 1, expected, phase)
    assert key in controller.jobs
    try:
        await asyncio.wait_for(controller.jobs[key], 4)
    finally:
        await controller.stop_game(game)
    assert attempts == 3 and len(destination.sent) == 1
    reference = persistence.load_state(game.guild_id)["gameplay"]["panels"]["1"]
    assert reference == {"channel_id": 101, "message_id": 1}
    assert isinstance(destination.messages[1].view, DayPanel if phase == "day" else NightPanel)
    await send_panels(controller, game, phase)
    assert len(destination.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["night", "day"])
@pytest.mark.parametrize("next_status", [403, 503])
async def test_panel_retry_stops_after_blocked_dm_or_phase_change(world, monkeypatch, phase, next_status):
    game, controller, destination = setup_phase(world, phase)
    expected = st.identity(game)
    retry_entered = asyncio.Event()
    attempts = 0
    async def unavailable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        status = 503 if attempts == 1 else next_status
        if attempts > 1:
            retry_entered.set()
        error_type = discord.Forbidden if status == 403 else discord.HTTPException
        raise error_type(S(status=status, reason="offline test"), "delivery unavailable")
    monkeypatch.setattr(destination, "send", unavailable)
    key = (game.guild_id, "panel", 1, expected, phase)
    scheduled = []
    original_start_job = controller.start_job
    def track_job(job_key, factory):
        task = original_start_job(job_key, factory)
        if job_key == key:
            scheduled.append(task)
        return task
    monkeypatch.setattr(controller, "start_job", track_job)
    await send_panels(controller, game, phase)
    # A blocked retry can finish while the other players' panels are sent.
    assert len(scheduled) == 1
    task = scheduled[0]
    try:
        await asyncio.wait_for(retry_entered.wait(), 2)
        if next_status == 503:
            game.phase = "day" if phase == "night" else "night"
            game.day_number += 1
            game.gameplay["night_token"] = "next-night"
        await asyncio.wait_for(task, 3)
    finally:
        await controller.stop_game(game)
    assert attempts == 2 and not destination.sent
    assert "1" not in game.gameplay["panels"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["night", "day"])
async def test_initially_blocked_panel_does_not_schedule_endless_retries(world, monkeypatch, phase):
    game, controller, destination = setup_phase(world, phase)
    async def blocked(*args, **kwargs):
        raise discord.Forbidden(S(status=403, reason="blocked"), "offline test")
    monkeypatch.setattr(destination, "send", blocked)
    await send_panels(controller, game, phase)
    assert not controller.jobs and not destination.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["night", "day"])
async def test_bulk_panel_delivery_keeps_original_phase_when_lookup_advances_game(world, monkeypatch, phase):
    game, controller, _ = setup_phase(world, phase)
    _, guild, _, channels, _ = world
    game.player_roles[2] = "Deputy"
    game.role_states[2] = {"deputy_shots_remaining": 1}
    changed = False
    async def lookup(server, uid):
        nonlocal changed
        if not changed:
            changed = True
            game.phase = "day" if phase == "night" else "night"
            game.day_number += 1
            game.gameplay["night_token"] = "next-night"
        return guild.get_member(uid)
    monkeypatch.setattr(game, "get_member_safe", lookup)
    await send_panels(controller, game, phase)
    assert changed
    assert all(not channel.sent for cid, channel in channels.items() if cid >= 100)
    assert not game.gameplay["panels"] and not controller.jobs

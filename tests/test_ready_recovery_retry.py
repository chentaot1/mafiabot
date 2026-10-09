"""READY recovery follows the current guild cache across reconnects."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from config import PLAYING_ROLE_ID
from gameplay import state as st
from gameplay.controller import Controller
from test_gameplay import world, Channel
from test_startup_access_recovery import Principal, configure


def replacement_guild(old, roles):
    members = [Principal(member.id) for member in old.members]
    guild = S(id=old.id, members=members, default_role=roles[0], me=Principal(999),
              get_role=roles.get,
              get_member=lambda uid: next((m for m in members if m.id == uid), None))
    channels = {cid: Channel(cid, guild) for cid in (10, 11, 12, 13)}
    guild.get_channel = channels.get
    for channel in channels.values():
        channel.edit = AsyncMock()
    return guild, channels


@pytest.fixture
def ready_recovery(world, monkeypatch):
    import bot as module
    game, guild, channels, members, roles = configure(world)
    game.gameplay['startup']['complete'] = True
    game.persist_now()
    current = [guild]
    client = S(user=S(id=999), guilds=[guild], commands=[], intents=S(), db=object(),
               tree=S(sync=AsyncMock(return_value=[]), get_commands=lambda **kwargs: []),
               get_guild=lambda gid: current[0], _mafia_dm_outbox_started=True,
               _mafia_full_reconnect=True)
    controller = Controller(client)
    client.gameplay_controller = controller
    controller.recover = AsyncMock()
    monkeypatch.setattr(module, 'bot', client)
    monkeypatch.setattr(gm, '_BOT', client)
    monkeypatch.setattr(module, 'active_games', gm.active_games)
    monkeypatch.setattr(module, 'ALLOWED_GUILD_ID', game.guild_id)
    monkeypatch.setattr(module, '_dbg', lambda *a, **kw: None)
    monkeypatch.setattr(module, '_ensure_gateway_watchdog_task', lambda: None)
    return module, client, current, game, guild, channels, members, roles


def gated_retry(monkeypatch):
    waiting, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    async def retry_delay(seconds):
        await real_sleep(0.01)
        waiting.set()
        await release.wait()
    monkeypatch.setattr(asyncio, 'sleep', retry_delay)
    return waiting, release


@pytest.mark.asyncio
@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('failure', ['missing_guild', 'missing_role', 'api_failure'])
async def test_ready_retry_uses_replacement_guild_and_preserves_match(ready_recovery, monkeypatch, cold, failure):
    module, client, current, original, guild, channels, members, roles = ready_recovery
    expected_roles = dict(original.player_roles)
    fresh, fresh_channels = replacement_guild(guild, dict(roles))
    if failure == 'missing_guild':
        current[0] = None
    elif failure == 'missing_role':
        roles.pop(original.alive_role_id)
    else:
        channels[11].edit.side_effect = discord.HTTPException(S(status=503, reason='unavailable'), 'retry')
    if cold:
        gm.active_games.clear()
    waiting, release = gated_retry(monkeypatch)
    try:
        await module.on_ready()
        assert controller_key(original) in client.gameplay_controller.jobs
        task = client.gameplay_controller.jobs[controller_key(original)]
        await asyncio.wait_for(waiting.wait(), 2)
        pending = gm.active_games.get(original.guild_id)
        if pending:
            with pytest.raises(st.Rejected):
                st.require_current(pending)
        current[0] = fresh
        release.set()
        await asyncio.wait_for(task, 2)
        restored = gm.active_games[original.guild_id]
        assert not restored._rehydrate_pending and not restored._recovering_permissions
        assert restored.game_key == original.game_key
        assert restored.player_roles == expected_roles
        assert restored.players == fresh.members
        if not cold:
            assert restored is original
        assert not restored.graveyard
        st.require_current(restored)
        client.gameplay_controller.recover.assert_awaited_once_with(restored)
        for member in fresh.members:
            assert member.add_roles.await_count == 3
            member.send.assert_not_awaited()
        for cid in (11, 12, 13):
            fresh_channels[cid].edit.assert_awaited_once()
        assert not client._mafia_full_reconnect
    finally:
        release.set()
        await client.gameplay_controller.stop_all()


def controller_key(game):
    return (game.guild_id, 'restore', 'current')


@pytest.mark.asyncio
async def test_repeated_ready_keeps_one_recovery_job(ready_recovery, monkeypatch):
    module, client, current, game, guild, _, members, roles = ready_recovery
    fresh, fresh_channels = replacement_guild(guild, dict(roles))
    roles.pop(game.alive_role_id)
    waiting, release = gated_retry(monkeypatch)
    try:
        await module.on_ready()
        await asyncio.wait_for(waiting.wait(), 2)
        task = client.gameplay_controller.jobs[controller_key(game)]
        await module.on_ready()
        assert list(client.gameplay_controller.jobs.values()) == [task]
        assert gm.active_games[game.guild_id] is game
        current[0] = fresh
        release.set()
        await asyncio.wait_for(task, 2)
        client.gameplay_controller.recover.assert_awaited_once_with(game)
        for member in members:
            member.add_roles.assert_not_awaited()
        for cid in (11, 12, 13):
            fresh_channels[cid].edit.assert_awaited_once()
    finally:
        release.set()
        await client.gameplay_controller.stop_all()


@pytest.mark.asyncio
async def test_reset_cancels_pending_ready_recovery(ready_recovery, monkeypatch):
    module, client, current, game, guild, channels, members, _ = ready_recovery
    current[0] = None
    waiting, release = gated_retry(monkeypatch)
    game._historical_reset = AsyncMock()
    try:
        await module.on_ready()
        await asyncio.wait_for(waiting.wait(), 2)
        task = client.gameplay_controller.jobs[controller_key(game)]
        await asyncio.wait_for(game.reset(guild), 2)
        assert task.cancelled()
        game._historical_reset.assert_awaited_once()
        assert not client.gameplay_controller.jobs
        current[0] = guild
        release.set()
        client.gameplay_controller.recover.assert_not_awaited()
        for member in members:
            member.add_roles.assert_not_awaited()
        for channel in channels.values():
            channel.edit.assert_not_awaited()
    finally:
        release.set()
        await client.gameplay_controller.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('disconnected', [False, True])
@pytest.mark.parametrize('failure', ['role', 'channel', 'public'])
async def test_command_startup_retry_follows_current_guild(ready_recovery, monkeypatch, failure, disconnected):
    module, client, current, game, guild, channels, members, roles = ready_recovery
    fifth = Principal(5)
    members.append(fifth)
    game.players, game.living_players = members[:], members[:]
    for member in members:
        member.send = AsyncMock(return_value=S(delete=AsyncMock()))
    game.in_progress = False
    game.setup_infrastructure = AsyncMock()
    client.db = None
    fresh, fresh_channels = replacement_guild(guild, dict(roles))
    if failure == 'role':
        roles.pop(PLAYING_ROLE_ID)
    elif failure == 'channel':
        channels.pop(11)
    else:
        channels[10].send = AsyncMock(side_effect=discord.HTTPException(
            S(status=503, reason='temporary failure'), 'retry'))
    draw_calls = []
    def draw(count, **kwargs):
        draw_calls.append(count)
        return ['Mobster', 'Doctor', 'Doctor', 'Doctor', 'Doctor']
    monkeypatch.setattr(module.game_roles, 'draw_roles_for_startgame', draw)
    ctx = S(guild=guild, channel=S(id=10), send=AsyncMock(), bot=client)
    waiting, release = gated_retry(monkeypatch)
    try:
        await module.startgame.callback(ctx)
        match, assigned = game.game_key, dict(game.player_roles)
        key = (game.guild_id, 'startup', match)
        assert key in client.gameplay_controller.jobs
        assert any('assignment is saved' in call.args[0] for call in ctx.send.call_args_list)
        if disconnected:
            current[0] = None
        await asyncio.wait_for(waiting.wait(), 2)
        current[0] = fresh
        release.set()
        await asyncio.wait_for(client.gameplay_controller.jobs[key], 2)
        assert gm.active_games[game.guild_id] is game
        assert game.game_key == match and game.player_roles == assigned
        assert game.gameplay['startup']['complete'] and game.gameplay['startup']['announced']
        assert draw_calls == [5] and not game.graveyard
        saved = persistence.load_state(game.guild_id)
        assert saved['game_key'] == match
        assert saved['player_roles'] == {str(uid): role for uid, role in assigned.items()}
        assert len(fresh_channels[10].sent) == 1
        assert len(fresh_channels[11].sent) == (0 if failure == 'public' else 1)
        for member in members:
            assert member.send.await_count == (2 if failure == 'public' else 1)
        for member in fresh.members:
            assert member.send.await_count == (0 if failure == 'public' else 1)
        client.gameplay_controller.recover.assert_awaited_once_with(game)
    finally:
        release.set()
        await client.gameplay_controller.stop_all()

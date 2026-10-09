"""Startup message progress survives fallback retries and shutdown."""
import asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from game import Game
from database import Database
from gameplay import startup
from gameplay.records import load_ui
from test_gameplay import world


def prepare(world, role='Psychic'):
    game, guild, controller, _, players = world
    game.player_roles[1] = role
    game.role_states[1] = {'exe_target': 2} if role == 'Executioner' else {'ga_target_id': 2}
    game.gameplay['startup'] = {'match': game.game_key, 'complete': False,
                               'announced': False, 'completed_players': []}
    game.persist_now()
    return game, guild, controller, players


async def restore(game, guild):
    recovered = Game.from_persisted(persistence.load_state(game.guild_id))
    await recovered.rehydrate_members(guild)
    gm.active_games[game.guild_id] = recovered
    return recovered


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('role,kind', [('Psychic', 'psychic_brief'), ('Executioner', 'exe_target'), ('Guardian Angel', 'ga_bind')])
async def test_partial_startup_dm_failure_retries_only_missing_message(world, monkeypatch, role, kind, restart):
    game, guild, _, players = prepare(world, role)
    sent, attempts = [], 0
    async def send(text):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise discord.HTTPException(S(status=503, reason='temporary failure'), 'retry')
        sent.append(text)
    monkeypatch.setattr(players[0], 'send', send)
    with pytest.raises(discord.HTTPException):
        await startup.deliver(game, guild, client=S(db=None))
    saved = persistence.load_state(game.guild_id)
    assert saved['gameplay']['startup']['dm_receipts']['1'] == ['role_deal']
    assert not saved['gameplay']['startup']['complete']
    if restart:
        game = await restore(game, guild)
    await startup.deliver(game, guild, client=S(db=None))
    assert game.gameplay['startup']['complete']
    assert game.gameplay['startup']['dm_receipts']['1'] == ['role_deal', kind]
    assert attempts == 3 and len(sent) == 2
    assert sum('Your role is:' in text for text in sent) == 1
    await startup.deliver(game, guild, client=S(db=None))
    assert attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['role_deal', 'psychic_brief'])
@pytest.mark.parametrize('stage', ['request', 'checkpoint'])
async def test_shutdown_saves_individual_startup_message_receipt(world, monkeypatch, kind, stage):
    game, guild, controller, players = prepare(world)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(text):
        sent.append(text)
        current_kind = 'role_deal' if 'Your role is:' in text else 'psychic_brief'
        if stage == 'request' and current_kind == kind:
            entered.set()
            await release.wait()
    monkeypatch.setattr(players[0], 'send', send)
    if stage == 'checkpoint':
        original_flush = game.persist_flush
        async def persist():
            if kind in game.gameplay['startup'].get('dm_receipts', {}).get('1', []):
                entered.set()
                await release.wait()
            await original_flush()
        monkeypatch.setattr(game, 'persist_flush', persist)
    async def initialize():
        async with game._startup_lock:
            await startup.deliver(game, guild, client=S(db=None))
    task = controller.start_job((game.guild_id, 'startup', game.game_key), initialize)
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(controller.stop_game(game))
        await asyncio.sleep(0)
        assert game._startup_lock.locked() and not shutdown.done()
        task.cancel()
        release.set()
        await asyncio.wait_for(shutdown, 3)
    finally:
        release.set()
        await controller.stop_game(game)
        if shutdown:
            await shutdown
    assert task.cancelled() and not game._startup_lock.locked()
    saved = persistence.load_state(game.guild_id)
    assert kind in saved['gameplay']['startup']['dm_receipts']['1']
    recovered = await restore(game, guild)
    await startup.deliver(recovered, guild, client=S(db=None))
    assert len(sent) == 2
    assert recovered.gameplay['startup']['complete']


@pytest.mark.asyncio
async def test_blocked_startup_dm_does_not_stall_other_players_and_is_not_retried(world):
    game, guild, _, players = prepare(world)
    players[0].send = AsyncMock(side_effect=discord.Forbidden(S(status=403, reason='blocked'), 'blocked'))
    await startup.deliver(game, guild, client=S(db=None))
    assert game.gameplay['startup']['complete']
    assert players[0].send.await_count == 2
    for member in players[1:]:
        assert member.send.await_count >= 1
    recovered = await restore(game, guild)
    await startup.deliver(recovered, guild, client=S(db=None))
    assert players[0].send.await_count == 2


@pytest.mark.parametrize('raw,expected', [
    (None, {}), (['role_deal'], {}),
    ({'1': ['role_deal', 'role_deal', False, None, 'x' * 33], '2': 'role_deal', 'bad': ['role_deal']}, {'1': ['role_deal']}),
])
def test_startup_message_receipts_are_validated_on_load(raw, expected):
    loaded = load_ui({'version': 1, 'startup': {'match': 'saved-match', 'dm_receipts': raw}})
    assert loaded['startup']['dm_receipts'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_queue', [False, True])
async def test_restart_transport_switch_does_not_repeat_accepted_role_message(world, tmp_path, monkeypatch, initial_queue):
    game, guild, _, players = prepare(world)
    db = Database(str(tmp_path / 'startup-outbox.db'))
    db.initialize()
    original_enqueue = db.enqueue_dm_outbox
    sent = []
    attempts = 0
    if initial_queue:
        def enqueue(**kwargs):
            if kwargs['target_user_id'] == 1 and kwargs['kind'] == 'psychic_brief':
                raise OSError('queue temporarily unavailable')
            return original_enqueue(**kwargs)
        monkeypatch.setattr(db, 'enqueue_dm_outbox', enqueue)
        failure = OSError
    else:
        failure = discord.HTTPException
    async def send(text):
        nonlocal attempts
        attempts += 1
        if not initial_queue and attempts == 2:
            raise discord.HTTPException(S(status=503, reason='temporary'), 'retry')
        sent.append(text)
    monkeypatch.setattr(players[0], 'send', send)
    with pytest.raises(failure):
        await startup.deliver(game, guild, client=S(db=db if initial_queue else None))
    recovered = await restore(game, guild)
    await startup.deliver(recovered, guild, client=S(db=None if initial_queue else db))
    with db._transaction() as conn:
        queued = conn.execute('SELECT kind, content, match_key FROM dm_outbox WHERE target_user_id=1').fetchall()
    assert len(queued) == len(sent) == 1
    assert queued[0]['match_key'] == game.game_key
    assert queued[0]['kind'] == ('role_deal' if initial_queue else 'psychic_brief')
    assert sum('Your role is:' in text for text in [sent[0], queued[0]['content']]) == 1
    assert recovered.gameplay['startup']['complete']


@pytest.mark.asyncio
async def test_shutdown_saves_queue_handoff_before_fallback_recovery(world, tmp_path, monkeypatch):
    game, guild, controller, players = prepare(world)
    db = Database(str(tmp_path / 'shutdown-outbox.db'))
    db.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    original_flush = game.persist_flush
    async def persist():
        if 'role_deal' in game.gameplay['startup'].get('dm_receipts', {}).get('1', []):
            entered.set()
            await release.wait()
        await original_flush()
    monkeypatch.setattr(game, 'persist_flush', persist)
    async def initialize():
        async with game._startup_lock:
            await startup.deliver(game, guild, client=S(db=db))
    task = controller.start_job((game.guild_id, 'startup', game.game_key), initialize)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(controller.stop_game(game))
        await asyncio.sleep(0)
        assert not shutdown.done() and game._startup_lock.locked()
        task.cancel()
        release.set()
        await asyncio.wait_for(shutdown, 3)
    finally:
        release.set()
        await controller.stop_game(game)
    recovered = await restore(game, guild)
    await startup.deliver(recovered, guild, client=S(db=None))
    with db._transaction() as conn:
        queued = conn.execute('SELECT kind FROM dm_outbox WHERE target_user_id=1').fetchall()
    assert [row['kind'] for row in queued] == ['role_deal']
    players[0].send.assert_awaited_once()
    assert 'Your role is:' not in players[0].send.call_args.args[0]
    assert task.cancelled() and recovered.gameplay['startup']['complete']

"""Temporary SQLite contention keeps saved startup jobs recoverable."""
import asyncio
import sqlite3
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

import database as database_module
import game as gm
import persistence
from database import Database
from gameplay import startup, state as st
from test_gameplay import world


@pytest.fixture
def fast_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'contention.db'))
    db.initialize()
    original_connect = sqlite3.connect
    def connect(*args, **kwargs):
        kwargs['timeout'] = 0.02
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(database_module.sqlite3, 'connect', connect)
    return db


def blocker(db):
    conn = sqlite3.connect(db.path)
    conn.execute('BEGIN IMMEDIATE')
    return conn


def gate_retries(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    async def pause(seconds):
        await original_sleep(0.01)
        entered.set()
        await release.wait()
    monkeypatch.setattr(asyncio, 'sleep', pause)
    return entered, release


def test_busy_writer_raises_retryable_error_and_preserves_original_code(fast_db):
    lock = blocker(fast_db)
    try:
        with pytest.raises(OSError) as failed:
            fast_db.enqueue_dm_outbox(guild_id=123, kind='role_deal', dedupe_key='saved-role',
                                     match_key='saved-match', target_user_id=1, content='private role')
        assert isinstance(failed.value.__cause__, sqlite3.OperationalError)
        assert failed.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
    finally:
        lock.close()
    fast_db.enqueue_dm_outbox(guild_id=123, kind='role_deal', dedupe_key='saved-role',
                             match_key='saved-match', target_user_id=1, content='private role')
    with fast_db._transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM dm_outbox').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_supervised_startup_survives_real_sqlite_lock(world, fast_db, monkeypatch, cancel):
    game, guild, controller, _, players = world
    game.phase = 'day'
    game.gameplay['startup'] = {'match': game.game_key, 'complete': False,
                               'announced': False, 'completed_players': []}
    expected_roles = dict(game.player_roles)
    game.persist_now()
    controller.bot.db = fast_db
    monkeypatch.setattr(controller, 'recover', AsyncMock())
    waiting, release = gate_retries(monkeypatch)
    lock = blocker(fast_db)
    task = controller.start_job((game.guild_id, 'startup', game.game_key),
                                lambda: startup.resume(game, guild, client=controller.bot))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not task.done() and not game.gameplay['startup']['complete']
        assert game.gameplay['startup'].get('dm_receipts', {}) == {}
        assert persistence.load_state(game.guild_id)['player_roles'] == {str(k): v for k, v in expected_roles.items()}
        with pytest.raises(st.Rejected):
            st.require_current(game)
        if cancel:
            await asyncio.wait_for(controller.stop_game(game), 2)
            assert task.cancelled()
        lock.close()
        lock = None
        release.set()
        if cancel:
            await startup.resume(game, guild, client=controller.bot)
        else:
            await asyncio.wait_for(task, 3)
        assert game.gameplay['startup']['complete']
        assert game.player_roles == expected_roles
        with fast_db._transaction() as conn:
            queued = conn.execute('SELECT target_user_id, kind FROM dm_outbox').fetchall()
        assert len(queued) == len(set((row['target_user_id'], row['kind']) for row in queued)) == 5
        for member in players:
            member.send.assert_not_awaited()
    finally:
        if lock:
            lock.close()
        release.set()
        await controller.stop_all()


@pytest.mark.asyncio
async def test_startgame_command_saves_assignment_and_retries_locked_queue(world, fast_db, monkeypatch):
    import bot as module
    game, guild, controller, _, players = world
    fifth = S(**vars(players[0]))
    fifth.id, fifth.display_name = 5, 'Player 5'
    fifth.send, fifth.add_roles = AsyncMock(), AsyncMock()
    players.append(fifth)
    game.players = players[:]
    for member in players:
        member.send = AsyncMock(return_value=S(delete=AsyncMock()))
    game.in_progress = False
    game.setup_infrastructure = AsyncMock()
    controller.bot.db = fast_db
    controller.recover = AsyncMock()
    monkeypatch.setattr(module, 'bot', controller.bot)
    monkeypatch.setattr(module, 'active_games', gm.active_games)
    monkeypatch.setattr(module, 'ALLOWED_GUILD_ID', game.guild_id)
    monkeypatch.setattr(module.game_roles, 'draw_roles_for_startgame', lambda count, **kw: ['Doctor'] * count)
    ctx = S(guild=guild, channel=S(id=10), send=AsyncMock(), bot=controller.bot)
    waiting, release = gate_retries(monkeypatch)
    lock = blocker(fast_db)
    try:
        await module.startgame.callback(ctx)
        assert any('assignment is saved' in call.args[0] for call in ctx.send.call_args_list)
        assigned = dict(game.player_roles)
        assert len(assigned) == 5
        assert not game.gameplay['startup']['complete']
        task = controller.jobs[(game.guild_id, 'startup', game.game_key)]
        await asyncio.wait_for(waiting.wait(), 2)
        lock.close()
        lock = None
        release.set()
        await asyncio.wait_for(task, 3)
        assert game.player_roles == assigned and game.gameplay['startup']['complete']
        with fast_db._transaction() as conn:
            assert conn.execute('SELECT COUNT(*) FROM dm_outbox').fetchone()[0] == 5
        for member in players:
            member.send.assert_awaited_once()
    finally:
        if lock:
            lock.close()
        release.set()
        await controller.stop_all()


@pytest.mark.parametrize('sql', ['INVALID SQL', 'SELECT * FROM missing_table'])
def test_permanent_sql_errors_keep_original_type(fast_db, sql):
    with pytest.raises(sqlite3.OperationalError) as failed:
        with fast_db._transaction() as conn:
            conn.execute(sql)
    assert failed.value.sqlite_errorcode == sqlite3.SQLITE_ERROR


def test_snapshot_upgrade_contention_is_retryable(fast_db):
    with pytest.raises(OSError) as failed:
        with fast_db._transaction() as stale:
            stale.execute('BEGIN')
            stale.execute('SELECT COUNT(*) FROM dm_outbox').fetchone()
            fast_db.enqueue_dm_outbox(guild_id=123, kind='role_deal', dedupe_key='other-write',
                                     target_user_id=1, content='private role')
            stale.execute("UPDATE dm_outbox SET status='sent'")
    assert failed.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
    with fast_db._transaction() as conn:
        assert conn.execute('SELECT status FROM dm_outbox').fetchone()[0] == 'pending'


def test_locked_table_is_retryable_and_connection_is_closed(fast_db):
    with pytest.raises(OSError) as failed:
        with fast_db._transaction() as conn:
            conn.execute('CREATE TABLE local_lock(value)')
            conn.executemany('INSERT INTO local_lock VALUES (?)', [(1,), (2,)])
            cursor = conn.execute('SELECT * FROM local_lock')
            assert cursor.fetchone()[0] == 1
            conn.execute('DROP TABLE local_lock')
    assert failed.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_LOCKED
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_connection_setup_contention_is_retryable_and_closes_partial_connection(fast_db, tmp_path, monkeypatch):
    path = str(tmp_path / 'connection-setup.db')
    lock = sqlite3.connect(path)
    lock.execute('CREATE TABLE existing(value)')
    lock.commit()
    lock.execute('BEGIN EXCLUSIVE')
    opened = []
    original_connect = sqlite3.connect
    def track(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        opened.append(conn)
        return conn
    monkeypatch.setattr(database_module.sqlite3, 'connect', track)
    try:
        with pytest.raises(OSError) as failed:
            with Database(path)._transaction():
                pytest.fail('Connection setup should be waiting for the database lock')
        assert failed.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')
    finally:
        lock.close()


def test_readonly_database_error_is_not_retried(fast_db, monkeypatch):
    from pathlib import Path
    conn = sqlite3.connect(Path(fast_db.path).as_uri() + '?mode=ro', uri=True)
    monkeypatch.setattr(fast_db, '_conn', lambda: conn)
    with pytest.raises(sqlite3.OperationalError) as failed:
        fast_db.enqueue_dm_outbox(guild_id=123, kind='role_deal', dedupe_key='readonly',
                                 target_user_id=1, content='private role')
    assert failed.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_damaged_database_error_is_not_retried(tmp_path):
    path = tmp_path / 'damaged.db'
    path.write_bytes(b'invalid SQLite database kept for recovery')
    with pytest.raises(sqlite3.DatabaseError) as failed:
        Database(str(path)).initialize()
    assert failed.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert path.read_bytes() == b'invalid SQLite database kept for recovery'


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
async def test_prefix_busy_error_explains_retry_without_database_details(monkeypatch, wrapped):
    from discord.ext import commands
    from database import DatabaseBusy
    import bot_app.shared as shared
    import errors
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(shared, 'safe_reply', reply)
    original = DatabaseBusy('private database path and SQL details')
    error = commands.CommandInvokeError(original) if wrapped else original
    await errors.on_command_error(S(command='stats'), error)
    reply.assert_awaited_once()
    message = reply.call_args.args[1]
    assert 'try again' in message.lower() and 'busy' in message.lower()
    assert 'private database' not in message


@pytest.mark.asyncio
@pytest.mark.parametrize('deferred', [False, True])
async def test_slash_busy_error_explains_retry_privately(monkeypatch, deferred):
    from discord import app_commands
    from database import DatabaseBusy
    import errors
    interaction = S(response=S(is_done=lambda: deferred, send_message=AsyncMock()),
                    followup=S(send=AsyncMock()))
    error = app_commands.CommandInvokeError(S(name='stats'), DatabaseBusy('private database path'))
    await errors.on_app_command_tree_error(interaction, error)
    send = interaction.followup.send if deferred else interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.kwargs['ephemeral']
    assert 'try again' in send.call_args.args[0].lower()
    assert 'private database' not in send.call_args.args[0]

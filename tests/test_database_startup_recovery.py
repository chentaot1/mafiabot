"""Upgrade migration preserves complete history before publishing a ready DB."""
import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, Mock

import pytest

import persistence


@pytest.fixture
def legacy_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "__file__", str(tmp_path / "persistence.py"))
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path / "state")
    legacy = tmp_path / "bot_app" / "state" / "mafiabot.db"
    legacy.parent.mkdir(parents=True)
    return legacy, persistence.sqlite_db_path()


def create_history(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE saved_matches (game_key TEXT)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO saved_matches VALUES ('kept-match')")
    conn.commit()
    return conn


def history(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT game_key FROM saved_matches").fetchall()


@pytest.mark.parametrize("uncommitted", [False, True])
def test_migration_includes_committed_wal_records(legacy_tree, uncommitted):
    legacy, target = legacy_tree
    with closing(create_history(legacy)) as conn:
        if uncommitted:
            conn.execute("INSERT INTO saved_matches VALUES ('uncommitted-match')")
        assert Path(str(legacy) + "-wal").stat().st_size > 0
        persistence.migrate_legacy_sqlite_db()
        assert history(target) == [("kept-match",)]
        assert history(legacy) == [("kept-match",)]


def test_invalid_legacy_database_is_retained_without_publishing_a_replacement(legacy_tree):
    legacy, target = legacy_tree
    original = b"invalid SQLite history"
    legacy.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        persistence.migrate_legacy_sqlite_db()
    assert legacy.read_bytes() == original
    assert not target.exists()
    assert not list(target.parent.glob("mafiabot.db.tmp.*"))


def test_migration_never_replaces_existing_database(legacy_tree):
    legacy, target = legacy_tree
    target.parent.mkdir(parents=True)
    with closing(create_history(legacy)), closing(create_history(target)) as conn:
        conn.execute("INSERT INTO saved_matches VALUES ('new-match')")
        conn.commit()
        persistence.migrate_legacy_sqlite_db()
        assert history(target) == [("kept-match",), ("new-match",)]


def test_explicit_state_directory_does_not_import_unrelated_history(legacy_tree, tmp_path, monkeypatch):
    legacy, _ = legacy_tree
    override = tmp_path / "isolated-state"
    monkeypatch.setattr(persistence, "STATE_DIR", override)
    with closing(create_history(legacy)):
        persistence.migrate_legacy_sqlite_db()
    assert not persistence.sqlite_db_path().exists()


def test_failed_migration_leaves_no_partial_database_and_can_retry(legacy_tree, monkeypatch):
    legacy, target = legacy_tree
    with closing(create_history(legacy)):
        original = sqlite3.connect
        class BrokenSnapshot:
            def backup(self, destination):
                destination.execute("CREATE TABLE partial (value TEXT)")
                destination.commit()
                raise OSError("snapshot interrupted")
            def close(self):
                pass
        def connect(path, *args, **kwargs):
            if kwargs.get("uri"):
                return BrokenSnapshot()
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            with pytest.raises(OSError, match="snapshot interrupted"):
                persistence.migrate_legacy_sqlite_db()
        assert not target.exists()
        assert not list(target.parent.glob("mafiabot.db.tmp.*"))
        persistence.migrate_legacy_sqlite_db()
        assert history(target) == [("kept-match",)]


@pytest.fixture
def ready_client(monkeypatch):
    import bot
    client = S(user=S(id=999), guilds=[], commands=[], intents=S(), db=None,
        tree=S(sync=AsyncMock(return_value=[]), get_commands=lambda **kwargs: []),
        get_guild=lambda gid: None, _mafia_dm_outbox_started=True)
    monkeypatch.setattr(bot, "bot", client)
    monkeypatch.setattr(bot, "_dbg", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "_ensure_gateway_watchdog_task", lambda: None)
    monkeypatch.setattr(bot, "_restore_saved_game", AsyncMock())
    return bot, client


@pytest.mark.asyncio
async def test_ready_migrates_before_initializing_database(ready_client, monkeypatch):
    module, client = ready_client
    events = []
    monkeypatch.setattr(persistence, "migrate_legacy_sqlite_db", lambda: events.append("migrate"))
    db = S(initialize=lambda: events.append("initialize"))
    monkeypatch.setattr(module, "Database", lambda path: db)
    await module.on_ready()
    assert events == ["migrate", "initialize"]
    assert client.db is db


@pytest.mark.asyncio
async def test_ready_opens_migrated_history_with_current_schema(ready_client, legacy_tree):
    module, client = ready_client
    legacy, target = legacy_tree
    with closing(create_history(legacy)):
        await module.on_ready()
        assert client.db.path == str(target)
        assert history(target) == [("kept-match",)]
        with client.db._transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM dm_outbox").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_initialization_failure_can_retry_on_next_ready(ready_client, monkeypatch):
    module, client = ready_client
    initialize = Mock(side_effect=[OSError("database busy"), None])
    db = S(initialize=initialize)
    monkeypatch.setattr(persistence, "migrate_legacy_sqlite_db", lambda: None)
    monkeypatch.setattr(module, "Database", lambda path: db)
    await module.on_ready()
    assert client.db is None
    await module.on_ready()
    assert client.db is db
    assert initialize.call_count == 2


@pytest.mark.asyncio
async def test_failed_migration_does_not_create_an_empty_replacement(ready_client, monkeypatch):
    module, client = ready_client
    def unavailable():
        raise OSError("legacy history is temporarily unavailable")
    monkeypatch.setattr(persistence, "migrate_legacy_sqlite_db", unavailable)
    constructor = Mock()
    monkeypatch.setattr(module, "Database", constructor)
    await module.on_ready()
    constructor.assert_not_called()
    assert client.db is None


@pytest.mark.asyncio
async def test_ready_does_not_expose_database_before_initialization_finishes(ready_client, monkeypatch):
    module, client = ready_client
    started, release = asyncio.Event(), asyncio.Event()
    async def initialization(function, *args, **kwargs):
        if function is db.initialize:
            started.set()
            await release.wait()
        return function(*args, **kwargs)
    db = S(initialize=lambda: None)
    monkeypatch.setattr(persistence, "migrate_legacy_sqlite_db", lambda: None)
    monkeypatch.setattr(module, "Database", lambda path: db)
    monkeypatch.setattr(module, "run_blocking", initialization)
    ready = asyncio.create_task(module.on_ready())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert client.db is None
    finally:
        release.set()
        await ready
    assert client.db is db

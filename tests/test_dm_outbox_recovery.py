"""Queued DMs retain their receipts and retry budget across recovery barriers."""
import asyncio
import threading
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from database import Database
from test_gameplay import world


@pytest.fixture
def outbox(world, tmp_path, monkeypatch):
    import bot
    game, _, _, _, _ = world
    db = Database(str(tmp_path / "outbox.db"))
    db.initialize()
    monkeypatch.setattr(bot, "active_games", gm.active_games)
    return bot, game, db


def row(db):
    with db._transaction() as conn:
        return dict(conn.execute("SELECT * FROM dm_outbox").fetchone())


def enqueue(db, game, kind):
    return db.enqueue_dm_outbox(guild_id=game.guild_id, kind=kind,
        dedupe_key=f"mafia_{kind}:{game.guild_id}:{game.game_key}:1",
        match_key=game.game_key, target_user_id=1, content="private message")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["role_deal", "game_over"])
async def test_shutdown_drains_send_and_success_receipt(outbox, monkeypatch, kind):
    module, game, db = outbox
    enqueue(db, game, kind)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(text):
        started.set()
        await release.wait()

    user = S(send=AsyncMock(side_effect=send))
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False, get_user=lambda uid: user)
    monkeypatch.setattr(module, "bot", client)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        await asyncio.wait_for(started.wait(), 2)
        pump.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pump, 2)
    finally:
        release.set()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    user.send.assert_awaited_once_with("private message")
    saved = row(db)
    assert saved["status"] == "sent"
    assert saved["sent_at"] is not None
    assert saved["sending_since"] is None
    assert db.requeue_stale_dm_outbox_sending(stale_after_seconds=0) == 0
    assert db.claim_dm_outbox_batch() == []


@pytest.mark.asyncio
async def test_recovery_read_failure_does_not_exhaust_delivery_attempts(outbox, monkeypatch):
    module, game, db = outbox
    mid = enqueue(db, game, "role_deal")
    with db._transaction() as conn:
        conn.execute("UPDATE dm_outbox SET attempts=? WHERE id=?", (db.DM_OUTBOX_MAX_ATTEMPTS - 1, mid))
    gm.active_games.clear()

    def unreadable(gid):
        raise persistence.StateReadError("temporarily locked")

    monkeypatch.setattr(module, "load_state", unreadable)
    processed = threading.Event()
    for name in ("retry_dm_outbox_later", "defer_dm_outbox"):
        original = getattr(db, name)
        def tracked(*args, _original=original, **kwargs):
            _original(*args, **kwargs)
            processed.set()
        monkeypatch.setattr(db, name, tracked)
    user = S(send=AsyncMock())
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False, get_user=lambda uid: user)
    monkeypatch.setattr(module, "bot", client)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        assert await asyncio.to_thread(processed.wait, 2)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    user.send.assert_not_awaited()
    saved = row(db)
    assert saved["status"] == "pending"
    assert saved["attempts"] == db.DM_OUTBOX_MAX_ATTEMPTS - 1
    assert saved["last_error"] is None
    assert saved["not_before"] > saved["created_at"]

    # Once recovery succeeds, the same queued role message can be delivered.
    gm.active_games[game.guild_id] = game
    with db._transaction() as conn:
        conn.execute("UPDATE dm_outbox SET not_before=NULL WHERE id=?", (mid,))
    acknowledged = threading.Event()
    original_sent = db.mark_dm_outbox_sent
    def sent(msg_id):
        original_sent(msg_id)
        acknowledged.set()
    monkeypatch.setattr(db, "mark_dm_outbox_sent", sent)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        assert await asyncio.to_thread(acknowledged.wait, 2)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    user.send.assert_awaited_once_with("private message")
    assert row(db)["status"] == "sent"


@pytest.mark.asyncio
async def test_send_failure_still_schedules_delivery_retry(outbox, monkeypatch):
    module, game, db = outbox
    enqueue(db, game, "role_deal")
    error = discord.HTTPException(S(status=503, reason="service unavailable"), "test failure")
    user = S(send=AsyncMock(side_effect=error))
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False, get_user=lambda uid: user)
    monkeypatch.setattr(module, "bot", client)
    processed = threading.Event()
    original_retry = db.retry_dm_outbox_later
    def retry(*args, **kwargs):
        original_retry(*args, **kwargs)
        processed.set()
    monkeypatch.setattr(db, "retry_dm_outbox_later", retry)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        assert await asyncio.to_thread(processed.wait, 2)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    saved = row(db)
    assert saved["status"] == "pending"
    assert saved["attempts"] == 1
    assert saved["sent_at"] is None
    assert saved["last_error"] == "HTTPException"

"""Graceful shutdown releases unfinished batch claims without consuming attempts."""
import asyncio
import threading
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

from test_dm_outbox_recovery import outbox
from test_gameplay import world


def messages(db):
    with db._transaction() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM dm_outbox ORDER BY id")]


def enqueue_batch(db, game):
    for uid in (1, 2, 3):
        db.enqueue_dm_outbox(guild_id=game.guild_id, kind="role_deal",
            dedupe_key=f"mafia_role_deal:{game.guild_id}:{game.game_key}:{uid}",
            match_key=game.game_key, target_user_id=uid, content=f"private role {uid}")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "lookup", "send"])
async def test_cancelled_batch_is_immediately_available_for_next_pump(outbox, monkeypatch, stage):
    module, game, db = outbox
    enqueue_batch(db, game)
    claimed, unblock_claim = threading.Event(), threading.Event()
    entered, release = asyncio.Event(), asyncio.Event()
    users = {uid: S(send=AsyncMock()) for uid in (1, 2, 3)}
    if stage == "claim":
        original_claim = db.claim_dm_outbox_batch
        def paused_claim(**kwargs):
            rows = original_claim(**kwargs)
            claimed.set()
            unblock_claim.wait(3)
            return rows
        monkeypatch.setattr(db, "claim_dm_outbox_batch", paused_claim)
    async def lookup(uid):
        entered.set()
        await release.wait()
        return users[uid]
    if stage == "send":
        async def send(text):
            entered.set()
            await release.wait()
        users[1].send.side_effect = send
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False,
        get_user=lambda uid: None if stage == "lookup" else users[uid], fetch_user=lookup)
    monkeypatch.setattr(module, "bot", client)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        if stage == "claim":
            assert await asyncio.to_thread(claimed.wait, 2)
        else:
            await asyncio.wait_for(entered.wait(), 2)
        pump.cancel()
        unblock_claim.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pump, 3)
    finally:
        unblock_claim.set()
        release.set()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    saved = messages(db)
    expected = ["sent", "pending", "pending"] if stage == "send" else ["pending"] * 3
    assert [row["status"] for row in saved] == expected
    assert all(row["attempts"] == 0 and row["sending_since"] is None for row in saved)
    assert db.requeue_stale_dm_outbox_sending(stale_after_seconds=300) == 0
    # The next pump can claim pending rows immediately, without waiting for age.
    pending = db.claim_dm_outbox_batch()
    assert [row["target_user_id"] for row in pending] == ([2, 3] if stage == "send" else [1, 2, 3])
    if stage == "send":
        users[1].send.assert_awaited_once_with("private role 1")
    else:
        users[1].send.assert_not_awaited()
    users[2].send.assert_not_awaited()
    users[3].send.assert_not_awaited()


@pytest.mark.parametrize("settled", ["sent", "failed", "superseded"])
def test_release_batch_preserves_settled_rows_and_reassigned_claims(outbox, settled):
    _, game, db = outbox
    enqueue_batch(db, game)
    claimed = db.claim_dm_outbox_batch()
    if settled == "sent":
        db.mark_dm_outbox_sent(claimed[0]["id"])
    elif settled == "failed":
        with db._transaction() as conn:
            conn.execute("UPDATE dm_outbox SET attempts=? WHERE id=?", (db.DM_OUTBOX_MAX_ATTEMPTS - 1, claimed[0]["id"]))
        db.retry_dm_outbox_later(claimed[0]["id"], error="terminal", delay_seconds=120)
    else:
        db.mark_dm_outbox_superseded(claimed[0]["id"])
    db.retry_dm_outbox_later(claimed[1]["id"], error="temporary", delay_seconds=120)
    with db._transaction() as conn:
        conn.execute("UPDATE dm_outbox SET sending_since=? WHERE id=?", ("2099-01-01T00:00:00+00:00", claimed[2]["id"]))
    before = messages(db)
    assert db.release_dm_outbox_claims(claimed) == 0
    assert messages(db) == before
    assert db.release_dm_outbox_claims([]) == 0

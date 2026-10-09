"""Deferred night results retain their match identity and restart deduplication."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

import game as gm
import persistence
from engine import night
from test_dm_outbox_recovery import outbox
from test_gameplay import world


def messages(db):
    with db._transaction() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM dm_outbox ORDER BY id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("cold_recovery", [False, True])
@pytest.mark.parametrize("obsolete", [False, True])
async def test_deferred_night_result_keeps_exact_match_identity(outbox, monkeypatch, legacy, cold_recovery, obsolete):
    module, game, db = outbox
    match = "123:2026-10-09T01:23:45+00:00:original"
    game.game_key = match
    text = "Your private night result"
    if legacy:
        db.enqueue_dm_outbox(guild_id=123, kind="night_result",
            dedupe_key=f"mafia_night:123:{match}:1:1:123456", target_user_id=1, content=text)
    else:
        monkeypatch.setattr(gm, "_BOT", S(db=db))
        monkeypatch.setattr(game, "get_member_safe", AsyncMock(return_value=None))
        assert await night._dm_actor_id(game, S(), 1, text)
        assert messages(db)[0]["match_key"] == match
    if obsolete:
        game.game_key = "123:2026-10-09T02:34:56+00:00:replacement"
    if cold_recovery:
        persistence.save_state(123, game.to_persisted())
        gm.active_games.clear()
    user = S(send=AsyncMock())
    monkeypatch.setattr(module, "bot", S(get_user=lambda uid: user))
    await module._deliver_outbox_batch(db)
    saved = messages(db)[0]
    if obsolete:
        assert saved["status"] == "superseded"
        user.send.assert_not_awaited()
    else:
        if cold_recovery:
            assert saved["status"] == "pending" and saved["attempts"] == 0
            user.send.assert_not_awaited()
            gm.active_games[123] = game
            with db._transaction() as conn:
                conn.execute("UPDATE dm_outbox SET not_before=NULL")
            await module._deliver_outbox_batch(db)
        assert messages(db)[0]["status"] == "sent"
        user.send.assert_awaited_once_with(text)


def test_restarting_python_does_not_queue_same_night_result_twice(outbox, tmp_path):
    _, _, db = outbox
    source = """
import asyncio, sys
from types import SimpleNamespace as S
from unittest.mock import AsyncMock
from scripts.run_bounded_tests import offline_environment
offline_environment()
import game
from database import Database
from engine.night import _dm_actor_id
db = Database(sys.argv[1])
game._BOT = S(db=db)
model = S(guild_id=123, game_key='123:2026-10-09T01:23:45+00:00:original',
          day_number=1, get_member_safe=AsyncMock(return_value=None))
assert asyncio.run(_dm_actor_id(model, S(), 1, 'same private result'))
"""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment.update(MAFIABOT_OFFLINE_CHECKS="1", PYTHON_DOTENV_DISABLED="1",
                       MAFIABOT_STATE_DIR=str(tmp_path / "child-state"))
    for seed in ("11", "29"):
        environment["PYTHONHASHSEED"] = seed
        subprocess.run([sys.executable, "-c", source, db.path], env=environment,
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=20,
                       capture_output=True, text=True)
    assert len(messages(db)) == 1


@pytest.mark.asyncio
async def test_locked_night_result_queue_does_not_block_event_loop(outbox, monkeypatch):
    _, game, db = outbox
    monkeypatch.setattr(gm, "_BOT", S(db=db))
    monkeypatch.setattr(game, "get_member_safe", AsyncMock(return_value=None))
    entered, release = threading.Event(), threading.Event()
    def writer():
        with db._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entered.set()
            release.wait(3)
    worker = threading.Thread(target=writer)
    worker.start()
    assert await asyncio.to_thread(entered.wait, 2)
    delivery = asyncio.create_task(night._dm_actor_id(game, S(), 1, "private result"))
    started = time.monotonic()
    try:
        await asyncio.sleep(.05)
        assert time.monotonic() - started < .35
    finally:
        release.set()
        await asyncio.to_thread(worker.join, 2)
        await delivery
    assert len(messages(db)) == 1

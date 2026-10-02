"""B6.1: DM outbox SQLite round-trip (claim + dedupe)."""

from __future__ import annotations

import database as dbmod


def test_dm_outbox_enqueue_dedupe_and_claim(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "t.db")
    db = dbmod.Database(path)
    db.initialize()

    i1 = db.enqueue_dm_outbox(
        guild_id=1,
        kind="role_deal",
        dedupe_key="mafia_role_deal:1:gk:7",
        target_user_id=7,
        content="hello",
    )
    assert i1 is not None
    i2 = db.enqueue_dm_outbox(
        guild_id=1,
        kind="role_deal",
        dedupe_key="mafia_role_deal:1:gk:7",
        target_user_id=7,
        content="hello",
    )
    assert i2 is None

    rows = db.claim_dm_outbox_batch(limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == "sending"

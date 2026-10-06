"""mafiabot-t-restart-persist-test: persist → reload invariants (B3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import persistence
from game import Game


def test_persist_flush_roundtrip_isolated_state_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    g = Game(999_001)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.player_roles = {1: "Doctor", 2: "Mobster"}
    g.night_actions = {1: {"type": "heal", "target": 2, "actor": 1}}

    async def _flush() -> None:
        await g.persist_flush()

    asyncio.run(_flush())

    blob = persistence.load_state(999_001)
    assert blob is not None
    g2 = Game.from_persisted(blob)
    assert g2.guild_id == 999_001
    assert g2.in_progress is True
    assert g2.phase == "night"
    assert g2.day_number == 2
    assert g2.player_roles.get(1) == "Doctor"
    assert g2.night_actions.get(1, {}).get("type") == "heal"


def test_import_bot_has_supervisor_entrypoint() -> None:
    """Smoke-level signal: bot uses asyncio.run + _connect_forever (B1)."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "bot.py").read_text(encoding="utf-8")
    assert "async def _connect_forever" in src
    assert "asyncio.run(_run_session())" in src

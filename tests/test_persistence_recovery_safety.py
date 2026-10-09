"""Recovery files must survive rapid writes and temporary read failures."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

import persistence
from game_recovery import lobby_join_blocked_reason


def test_rapid_saves_keep_distinct_backup_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)

    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 9, tzinfo=timezone.utc)

    monkeypatch.setattr(persistence, "datetime", FrozenClock)
    for version in range(4):
        persistence.save_state(123, {"in_progress": True, "version": version})

    backups = list(tmp_path.glob("123.json.bak.*"))
    assert sorted(json.loads(path.read_text())["version"] for path in backups) == [0, 1, 2]
    assert persistence.load_state(123)["version"] == 3


def test_backup_retention_keeps_latest_versions_and_supports_legacy_names(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setenv("MAFIABOT_STATE_BACKUP_MAX", "2")
    legacy = tmp_path / "123.json.bak.20260101T000000Z"
    legacy.write_text('{"version": -1}')
    os.utime(legacy, (1, 1))
    for version in range(4):
        persistence.save_state(123, {"in_progress": True, "version": version})
        # Deterministic source mtimes distinguish backup ages without sleeps.
        os.utime(tmp_path / "123.json", (10 + version, 10 + version))
    assert sorted(json.loads(p.read_text())["version"] for p in tmp_path.glob("123.json.bak.*")) == [1, 2]


@pytest.mark.parametrize("kind", ["stats", "pending_endgame"])
def test_temporary_read_failure_blocks_recovery_without_quarantining(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    pending = {"outcome": "Town", "game_key": "unfinished-match"}
    if kind == "stats":
        persistence.save_stats(123, {"_meta": {"pending_endgame": pending}, "players": {"1": {"wins": 7}}})
        path = tmp_path / "123.stats.json"
        loader = persistence.load_stats
    else:
        persistence.save_pending_endgame_fallback(123, pending)
        path = tmp_path / "123.pending_endgame.json"
        loader = persistence.load_pending_endgame_fallback
    original = path.read_bytes()
    original_read = Path.read_text

    def locked_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporarily locked")
        return original_read(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", locked_read)
        with pytest.raises(persistence.StateReadError):
            loader(123)
        with pytest.raises(persistence.StateReadError):
            lobby_join_blocked_reason(123)
        if kind == "stats":
            with pytest.raises(persistence.StateReadError):
                persistence.save_stats_meta(123, {})
        assert path.read_bytes() == original
        assert not list(tmp_path.glob("*.corrupt*"))

    assert loader(123) is not None
    assert lobby_join_blocked_reason(123) is not None


@pytest.mark.parametrize("contents", ["[]", "null", '"not an object"', "{broken", "\udcff"])
def test_malformed_stats_are_quarantined(tmp_path, monkeypatch, contents):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    path = tmp_path / "123.stats.json"
    raw = contents.encode("utf-8", errors="surrogateescape")
    path.write_bytes(raw)
    assert persistence.load_stats(123) is None
    assert not path.exists()
    assert path.with_name("123.stats.json.corrupt").read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_prefix_recovery_error_tells_player_to_retry(monkeypatch, wrapped):
    from unittest.mock import AsyncMock
    from discord.ext import commands
    import bot_app.shared as shared
    import errors

    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(shared, "safe_reply", reply)
    original = persistence.StateReadError("private filesystem detail")
    error = commands.CommandInvokeError(original) if wrapped else original
    ctx = object()
    await errors.on_command_error(ctx, error)
    message = reply.call_args.args[1]
    assert "try again" in message.lower()
    assert "private filesystem detail" not in message
    reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_slash_recovery_error_is_private_and_explains_retry(monkeypatch, deferred):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from discord import app_commands
    import errors

    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: deferred, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    error = app_commands.CommandInvokeError(SimpleNamespace(name="join"), persistence.StateReadError("private filesystem detail"))
    await errors.on_app_command_tree_error(interaction, error)
    send = interaction.followup.send if deferred else interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.kwargs["ephemeral"] is True
    assert "try again" in send.call_args.args[0].lower()
    assert "private filesystem detail" not in send.call_args.args[0]

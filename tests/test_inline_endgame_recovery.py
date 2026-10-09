"""The last-resort stats marker survives checkpoints until explicitly cleared."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as S

import pytest

import game as gm
import game_recovery as recovery
import persistence
from database import Database
from game import Game
from test_gameplay import world


PENDING = {"game_key": "test-match", "outcome": "Town", "living_ids": [1, 2, 3, 4]}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_marker_added_after_snapshot_is_preserved_by_checkpoint(world, asynchronous):
    game, _, _, _, _ = world
    persistence.save_state(123, game.to_persisted())
    prepared = game.to_persisted()
    prepared["day_number"] = 2
    before = deepcopy(prepared)
    persistence.embed_pending_endgame_in_game_state(123, PENDING)
    if asynchronous:
        asyncio.run(persistence.save_state_async(123, prepared))
    else:
        persistence.save_state(123, prepared)
    saved = persistence.load_state(123)
    assert saved["_pending_endgame"] == PENDING
    assert saved["day_number"] == 2
    assert prepared == before


@pytest.mark.parametrize("operation", ["checkpoint", "embed", "clear"])
def test_unreadable_existing_state_is_not_replaced(world, monkeypatch, operation):
    game, _, _, _, _ = world
    persistence.save_state(123, game.to_persisted())
    persistence.embed_pending_endgame_in_game_state(123, PENDING)
    path = persistence.STATE_DIR / "123.json"
    original = path.read_bytes()
    original_read = Path.read_text
    def locked(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporarily locked")
        return original_read(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", locked)
        with pytest.raises(persistence.StateReadError):
            if operation == "checkpoint":
                game.persist_now()
            elif operation == "embed":
                persistence.embed_pending_endgame_in_game_state(123, PENDING)
            else:
                persistence.clear_inline_pending_endgame_from_game_state(123)
        assert path.read_bytes() == original
        assert not list(path.parent.glob("*.corrupt*"))
    assert persistence.load_state(123)["_pending_endgame"] == PENDING
    game.persist_now()
    assert persistence.load_state(123)["_pending_endgame"] == PENDING


def test_cleared_marker_is_not_resurrected_by_reloaded_model(world):
    game, _, _, _, _ = world
    persistence.save_state(123, game.to_persisted())
    persistence.embed_pending_endgame_in_game_state(123, PENDING)
    reloaded = Game.from_persisted(persistence.load_state(123))
    prepared = reloaded.to_persisted()
    recovery.clear_pending_endgame_meta(123)
    assert "_pending_endgame" not in persistence.load_state(123)
    persistence.save_state(123, prepared)
    assert "_pending_endgame" not in persistence.load_state(123)
    gm.active_games[123] = reloaded
    reloaded.persist_now()
    assert "_pending_endgame" not in persistence.load_state(123)


def test_preparing_game_snapshot_does_not_access_recovery_files(world, monkeypatch):
    game, _, _, _, _ = world
    reads = []
    def unavailable(*args):
        reads.append(args)
        raise AssertionError("Preparing a snapshot must not read saved state")
    monkeypatch.setattr(persistence, "load_state", unavailable)
    snapshot = game.to_persisted()
    assert snapshot["game_key"] == game.game_key
    assert snapshot["player_roles"] == {str(uid): role for uid, role in game.player_roles.items()}
    assert reads == []


@pytest.mark.parametrize("can_quarantine", [False, True])
def test_inline_embedding_preserves_damaged_recovery_evidence(world, monkeypatch, can_quarantine):
    _, _, _, _, _ = world
    path = persistence.STATE_DIR / "123.json"
    damaged = b"{recoverable saved-match fragment"
    path.write_bytes(damaged)
    original_rename = Path.rename
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporarily locked")
        return original_rename(self, *args, **kwargs)
    if can_quarantine:
        persistence.embed_pending_endgame_in_game_state(123, PENDING)
        assert path.with_name("123.json.corrupt").read_bytes() == damaged
        assert persistence.load_state(123) == {"_pending_endgame": PENDING}
    else:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", denied)
            with pytest.raises(persistence.StateReadError):
                persistence.embed_pending_endgame_in_game_state(123, PENDING)
        assert path.read_bytes() == damaged


def test_last_resort_marker_recovers_real_statistics_once_after_checkpoint(world, monkeypatch):
    game, _, _, _, _ = world
    db = Database(str(persistence.STATE_DIR / "recovery.db"))
    db.initialize()
    monkeypatch.setattr(gm, "_BOT", S(db=db))
    persistence.save_state(123, game.to_persisted())
    prepared = game.to_persisted()
    def failed(*args, **kwargs):
        raise OSError("temporary storage failure")
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit_endgame_atomic", failed)
        patch.setattr(recovery, "save_stats_meta", failed)
        patch.setattr(recovery, "save_pending_endgame_fallback", failed)
        patch.setattr(gm.time, "sleep", lambda *args: None)
        game._commit_endgame_stats(outcome="Town", living_ids=[1, 2, 3, 4])
    assert not game.stats_committed
    assert persistence.load_state(123)["_pending_endgame"] == PENDING
    persistence.save_state(123, prepared)
    assert recovery.commit_pending_endgame_before_state_delete(123)
    assert db.has_game_key(game.game_key)
    assert "_pending_endgame" not in persistence.load_state(123)
    before = db.get_player_stats_summary(guild_id=123, player_id=1)
    assert before["games_played"] == 1 and before["wins"] == 1
    assert not recovery.commit_pending_endgame_before_state_delete(123)
    assert db.get_player_stats_summary(guild_id=123, player_id=1) == before

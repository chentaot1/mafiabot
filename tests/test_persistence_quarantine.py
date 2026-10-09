"""Corruption recovery must preserve newer saves and earlier recovery evidence."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import persistence
from game_recovery import clear_pending_endgame_meta, lobby_join_blocked_reason


@pytest.fixture(params=["state", "stats"])
def snapshot(request, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    stats = request.param == "stats"
    return (tmp_path / ("123.stats.json" if stats else "123.json"),
        persistence.load_stats if stats else persistence.load_state,
        persistence.save_stats if stats else persistence.save_state)


def test_corrupt_reader_cannot_quarantine_a_newer_valid_save(snapshot, monkeypatch):
    path, load, save = snapshot
    damaged = "{damaged snapshot"
    path.write_text(damaged)
    decoding, release, writer_started, writer_finished = [threading.Event() for _ in range(4)]
    original_decode = json.loads

    def decode(data, *args, **kwargs):
        if data == damaged:
            decoding.set()
            assert release.wait(5), "reader was not released"
        return original_decode(data, *args, **kwargs)

    def write():
        writer_started.set()
        save(123, {"version": 2})
        writer_finished.set()

    monkeypatch.setattr(json, "loads", decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(load, 123)
        try:
            assert decoding.wait(3)
            writer = pool.submit(write)
            assert writer_started.wait(3)
            # Give an incorrectly unprotected writer time to overtake parsing.
            writer_finished.wait(0.1)
        finally:
            release.set()
        assert reader.result(timeout=3) is None
        writer.result(timeout=3)

    assert load(123) == {"version": 2}
    assert path.with_name(path.name + ".corrupt").read_text() == damaged


def test_repeated_corruption_keeps_each_recovery_copy(snapshot):
    path, load, _ = snapshot
    damaged = [b"{first damaged save", b"{second damaged save"]
    for contents in damaged:
        path.write_bytes(contents)
        assert load(123) is None
    archives = list(path.parent.glob(path.name + ".corrupt*"))
    assert sorted(p.read_bytes() for p in archives) == sorted(damaged)


@pytest.mark.parametrize("previous_copy", [False, True])
def test_failed_quarantine_preserves_original_and_defers_recovery(snapshot, monkeypatch, previous_copy):
    path, load, _ = snapshot
    damaged = b"{recoverable fragment"
    path.write_bytes(damaged)
    archive = path.with_name(path.name + ".corrupt")
    if previous_copy:
        archive.write_bytes(b"{earlier recovery copy")
    original_rename = Path.rename
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporarily locked")
        return original_rename(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", denied)
        with pytest.raises(persistence.StateReadError):
            load(123)
        with pytest.raises(persistence.StateReadError):
            lobby_join_blocked_reason(123)
        assert path.read_bytes() == damaged
        assert [p.read_bytes() for p in path.parent.glob(path.name + ".corrupt*")] == (
            [b"{earlier recovery copy"] if previous_copy else [])
    assert load(123) is None
    assert sorted(p.read_bytes() for p in path.parent.glob(path.name + ".corrupt*")) == sorted(
        [b"{earlier recovery copy", damaged] if previous_copy else [damaged])


def test_metadata_updates_can_read_under_the_same_guild_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    pending = {"game_key": "completed", "outcome": "Town"}
    players = {"1": {"games_played": 2, "wins": 1}}
    persistence.save_stats(123, {"players": players, "_meta": {"pending_endgame": pending}})
    persistence.save_stats_meta(123, {"pending_endgame": pending, "last_json_game_key": "completed"})
    clear_pending_endgame_meta(123)
    assert persistence.load_stats(123) == {"players": players, "_meta": {"last_json_game_key": "completed"}}


def test_another_guild_can_save_while_corrupt_reader_is_busy(snapshot, monkeypatch):
    path, load, save = snapshot
    damaged = "{damaged snapshot"
    path.write_text(damaged)
    decoding, release = threading.Event(), threading.Event()
    original_decode = json.loads
    def decode(data, *args, **kwargs):
        if data == damaged:
            decoding.set()
            assert release.wait(5)
        return original_decode(data, *args, **kwargs)
    monkeypatch.setattr(json, "loads", decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(load, 123)
        try:
            assert decoding.wait(3)
            pool.submit(save, 456, {"version": 3}).result(timeout=2)
        finally:
            release.set()
        assert reader.result(timeout=3) is None
    assert load(456) == {"version": 3}

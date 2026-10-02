from __future__ import annotations

from pathlib import Path

import pytest

import database as db_module


def test_db_idempotent_begin_game_commit(tmp_path: Path) -> None:
    db_path = str(tmp_path / "mafiabot.db")
    db = db_module.Database(db_path)
    db.initialize()

    is_first, game_id = db.begin_game_commit(
        guild_id=123,
        game_key="123:2026-01-01T00:00:00+00:00:abc",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T01:00:00+00:00",
        outcome="Town",
        player_count=2,
        ended_day_number=3,
        ended_phase="day",
    )
    assert is_first is True
    assert isinstance(game_id, int) and game_id > 0

    is_first2, game_id2 = db.begin_game_commit(
        guild_id=123,
        game_key="123:2026-01-01T00:00:00+00:00:abc",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T01:00:00+00:00",
        outcome="Town",
        player_count=2,
        ended_day_number=3,
        ended_phase="day",
    )
    assert is_first2 is False
    assert game_id2 == game_id


@pytest.mark.parametrize(
    "stats_data, expected_imported",
    [
        ({"players": {}}, 0),
        ({"players": {"10": {"games_played": "3", "wins": "2", "losses": "1", "draws": "0"}}}, 1),
        (
            {
                "players": {
                    "10": {"games_played": "3", "wins": "2", "losses": "1", "draws": "0"},
                    "bad_id": {"games_played": 1, "wins": 1, "losses": 0, "draws": 0},
                    "11": {"games_played": "not_a_number", "wins": 0, "losses": 0, "draws": 0},
                    "12": "not a dict",
                }
            },
            1,
        ),
    ],
)
def test_importer_skips_malformed_rows(tmp_path: Path, stats_data: dict, expected_imported: int) -> None:
    db_path = str(tmp_path / "mafiabot.db")
    db = db_module.Database(db_path)
    db.initialize()

    imported = db.import_player_stats_from_json(guild_id=123, stats_data=stats_data)
    assert imported == expected_imported

    if expected_imported:
        s10 = db.get_player_stats_summary(guild_id=123, player_id=10)
        assert s10 is not None
        assert int(s10["games_played"]) == 3
        assert int(s10["wins"]) == 2

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import config
from stats_personal import migrate_personal_wins_dict

# Faction breakdown for `get_player_stats_summary` must stay aligned with `config.TOWN_ROLES` /
# `config.ALL_MAFIA_ROLES` (same source as endgame JSON stats) so new Town roles are not mis-bucketed.
_TOWN_ROLES_SET = frozenset(config.TOWN_ROLES)
_MAFIA_ROLES_SET = frozenset(config.ALL_MAFIA_ROLES)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class LeaderboardRow:
    player_id: int
    value: float
    games_played: int
    wins: int


class Database:
    """
    SQLite-backed stats + match history for a single-guild bot.

    Pattern intentionally mirrors StudyBot:
    - connection-per-operation
    - WAL mode
    - schema + lightweight migrations in initialize()
    """

    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def _transaction(self):
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        _ensure_parent_dir(self.path)
        # timeout avoids transient "database is locked" failures on Windows/slow disks
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    def initialize(self) -> None:
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    game_key TEXT NOT NULL UNIQUE,
                    started_at TEXT,
                    ended_at TEXT,
                    outcome TEXT,
                    player_count INTEGER,
                    ended_day_number INTEGER,
                    ended_phase TEXT
                );

                CREATE TABLE IF NOT EXISTS game_players (
                    game_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    role_start TEXT,
                    role_end TEXT,
                    faction_start TEXT,
                    faction_end TEXT,
                    survived INTEGER,
                    died_day INTEGER,
                    death_cause TEXT,
                    UNIQUE(game_id, player_id),
                    FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS player_stats (
                    guild_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    games_played INTEGER DEFAULT 0,
                    wins_total INTEGER DEFAULT 0,
                    losses_total INTEGER DEFAULT 0,
                    draws_total INTEGER DEFAULT 0,
                    wins_town INTEGER DEFAULT 0,
                    wins_mafia INTEGER DEFAULT 0,
                    wins_arsonist INTEGER DEFAULT 0,
                    last_game_at TEXT,
                    PRIMARY KEY(guild_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS player_personal_stats (
                    guild_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY(guild_id, player_id, key)
                );

                CREATE TABLE IF NOT EXISTS player_role_stats (
                    guild_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    played INTEGER DEFAULT 0,
                    wins_total INTEGER DEFAULT 0,
                    losses_total INTEGER DEFAULT 0,
                    PRIMARY KEY(guild_id, player_id, role)
                );

                CREATE TABLE IF NOT EXISTS guild_stats_board (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT
                );
                """
            )

            # Lightweight forward-only migrations (idempotent).
            #
            # Audit H5 — only swallow the specific "duplicate column" error.
            # The prior code swallowed ALL OperationalError values, which
            # would hide a real migration bug (bad DDL, permissions, locked
            # DB) behind a successful-looking initialize().
            migrations: list[tuple[str, str, str]] = [
                ("games", "ended_phase", "TEXT"),
                ("game_players", "death_cause", "TEXT"),
            ]
            for table, col, col_def in migrations:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "duplicate column" in msg:
                        continue
                    raise

        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_games_guild_ended ON games(guild_id, ended_at);
                CREATE INDEX IF NOT EXISTS idx_games_guild_outcome ON games(guild_id, outcome);
                CREATE INDEX IF NOT EXISTS idx_player_stats_wins ON player_stats(guild_id, wins_total);
                CREATE INDEX IF NOT EXISTS idx_player_stats_games ON player_stats(guild_id, games_played);
                CREATE INDEX IF NOT EXISTS idx_personal_key ON player_personal_stats(guild_id, key, count);
                CREATE INDEX IF NOT EXISTS idx_role_stats_role ON player_role_stats(guild_id, role, wins_total);

                CREATE TABLE IF NOT EXISTS dm_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT,
                    match_key TEXT,
                    target_user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    not_before TEXT,
                    sending_since TEXT,
                    sent_at TEXT
                );
                DROP INDEX IF EXISTS idx_dm_outbox_dedupe;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_dm_outbox_dedupe_guild
                    ON dm_outbox(guild_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_dm_outbox_pending
                    ON dm_outbox(status, not_before, id);

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('dm_outbox_schema', '1');
                """
            )

            columns = {row[1] for row in conn.execute('PRAGMA table_info(dm_outbox)')}
            if 'match_key' not in columns:
                conn.execute('ALTER TABLE dm_outbox ADD COLUMN match_key TEXT')

    # --------------------
    # Write helpers
    # --------------------
    def begin_game_commit(
        self,
        *,
        guild_id: int,
        game_key: str,
        started_at: Optional[str],
        ended_at: Optional[str],
        outcome: str,
        player_count: int,
        ended_day_number: Optional[int],
        ended_phase: Optional[str],
    ) -> tuple[bool, int]:
        """
        Idempotent insert into games table.

        Returns (is_first_insert, game_id).
        """
        with self._transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO games(
                    guild_id, game_key, started_at, ended_at, outcome, player_count, ended_day_number, ended_phase
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    str(game_key),
                    started_at,
                    ended_at,
                    str(outcome),
                    int(player_count),
                    int(ended_day_number) if ended_day_number is not None else None,
                    str(ended_phase) if ended_phase is not None else None,
                ),
            )
            is_first = cur.rowcount == 1
            row = conn.execute("SELECT id FROM games WHERE game_key=?", (str(game_key),)).fetchone()
            if not row:
                raise RuntimeError("Failed to fetch game id after insert/ignore.")
            return is_first, int(row["id"])

    def has_game_key(self, game_key: str) -> bool:
        """True if this endgame was already committed to SQLite (idempotent retry)."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM games WHERE game_key=? LIMIT 1",
                (str(game_key),),
            ).fetchone()
            return row is not None

    def insert_game_players(
        self,
        *,
        game_id: int,
        guild_id: int,
        rows: Iterable[dict],
    ) -> None:
        with self._transaction() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO game_players(
                    game_id, guild_id, player_id,
                    role_start, role_end, faction_start, faction_end,
                    survived, died_day, death_cause
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(game_id),
                        int(guild_id),
                        int(r["player_id"]),
                        r.get("role_start"),
                        r.get("role_end"),
                        r.get("faction_start"),
                        r.get("faction_end"),
                        int(r.get("survived", 0)),
                        int(r["died_day"]) if r.get("died_day") is not None else None,
                        r.get("death_cause"),
                    )
                    for r in rows
                ],
            )

    # Legacy split-write deltas — production endgame uses commit_endgame_atomic (#26).
    def upsert_player_stats_delta(
        self,
        *,
        guild_id: int,
        player_id: int,
        games_played: int,
        wins_total: int,
        losses_total: int,
        draws_total: int,
        wins_town: int,
        wins_mafia: int,
        wins_arsonist: int,
        last_game_at: Optional[str] = None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_stats(
                    guild_id, player_id,
                    games_played, wins_total, losses_total, draws_total,
                    wins_town, wins_mafia, wins_arsonist,
                    last_game_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, player_id) DO UPDATE SET
                    games_played = CASE WHEN (games_played + excluded.games_played) < 0 THEN 0 ELSE (games_played + excluded.games_played) END,
                    wins_total   = CASE WHEN (wins_total   + excluded.wins_total)   < 0 THEN 0 ELSE (wins_total   + excluded.wins_total)   END,
                    losses_total = CASE WHEN (losses_total + excluded.losses_total) < 0 THEN 0 ELSE (losses_total + excluded.losses_total) END,
                    draws_total  = CASE WHEN (draws_total  + excluded.draws_total)  < 0 THEN 0 ELSE (draws_total  + excluded.draws_total)  END,
                    wins_town    = CASE WHEN (wins_town    + excluded.wins_town)    < 0 THEN 0 ELSE (wins_town    + excluded.wins_town)    END,
                    wins_mafia   = CASE WHEN (wins_mafia   + excluded.wins_mafia)   < 0 THEN 0 ELSE (wins_mafia   + excluded.wins_mafia)   END,
                    wins_arsonist= CASE WHEN (wins_arsonist+ excluded.wins_arsonist)< 0 THEN 0 ELSE (wins_arsonist+ excluded.wins_arsonist) END,
                    last_game_at = COALESCE(excluded.last_game_at, player_stats.last_game_at)
                """,
                (
                    int(guild_id),
                    int(player_id),
                    int(games_played),
                    int(wins_total),
                    int(losses_total),
                    int(draws_total),
                    int(wins_town),
                    int(wins_mafia),
                    int(wins_arsonist),
                    last_game_at,
                ),
            )

    def upsert_player_role_stats_delta(
        self,
        *,
        guild_id: int,
        player_id: int,
        role: str,
        played: int,
        wins_total: int,
        losses_total: int,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_role_stats(
                    guild_id, player_id, role,
                    played, wins_total, losses_total
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, player_id, role) DO UPDATE SET
                    played      = CASE WHEN (played      + excluded.played)      < 0 THEN 0 ELSE (played      + excluded.played)      END,
                    wins_total   = CASE WHEN (wins_total   + excluded.wins_total) < 0 THEN 0 ELSE (wins_total   + excluded.wins_total) END,
                    losses_total = CASE WHEN (losses_total + excluded.losses_total)< 0 THEN 0 ELSE (losses_total + excluded.losses_total)END
                """,
                (int(guild_id), int(player_id), str(role), int(played), int(wins_total), int(losses_total)),
            )

    def upsert_personal_win_delta(self, *, guild_id: int, player_id: int, key: str, delta: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_personal_stats(guild_id, player_id, key, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, player_id, key) DO UPDATE SET
                    count = CASE
                        WHEN (count + excluded.count) < 0 THEN 0
                        ELSE (count + excluded.count)
                    END
                """,
                (int(guild_id), int(player_id), str(key), int(delta)),
            )

    def commit_endgame_atomic(
        self,
        *,
        guild_id: int,
        game_key: str,
        started_at: Optional[str],
        ended_at: Optional[str],
        outcome: str,
        player_count: int,
        ended_day_number: Optional[int],
        ended_phase: Optional[str],
        game_player_rows: Iterable[dict],
        player_stats_deltas: Iterable[dict],
        role_stats_deltas: Iterable[dict],
        personal_win_deltas: Iterable[dict],
    ) -> tuple[bool, int]:
        """Audit H1+H2 — single-transaction endgame commit.

        Performs the games-row insert, per-player game_players inserts,
        and all aggregate stat upserts inside a single ``with conn:``
        block. On any exception, sqlite3's connection context manager
        rolls back the whole transaction, so a retry sees ``is_first=True``
        and can resume cleanly.

        Returns ``(is_first, game_id)``. If the game_key already exists,
        ``is_first=False`` and the rest of the parameters are ignored.

        Each delta dict carries the kwargs the corresponding upsert_*
        method would normally take (without the ``guild_id``, which is
        passed once at the top level).
        """
        with self._transaction() as conn:
            # games row — INSERT OR IGNORE then SELECT to get id; if a
            # prior commit already inserted the row, return early.
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO games(
                    guild_id, game_key, started_at, ended_at,
                    outcome, player_count, ended_day_number, ended_phase
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    str(game_key),
                    started_at,
                    ended_at,
                    str(outcome),
                    int(player_count),
                    int(ended_day_number) if ended_day_number is not None else None,
                    str(ended_phase) if ended_phase is not None else None,
                ),
            )
            is_first = cur.rowcount == 1
            row = conn.execute(
                "SELECT id FROM games WHERE game_key=?",
                (str(game_key),),
            ).fetchone()
            if not row:
                raise RuntimeError("Failed to fetch game id after insert/ignore.")
            game_id = int(row["id"])
            if not is_first:
                return False, game_id

            # game_players rows (INSERT OR IGNORE keeps the call idempotent
            # if the caller ever re-runs after a partial earlier success).
            conn.executemany(
                """
                INSERT OR IGNORE INTO game_players(
                    game_id, guild_id, player_id,
                    role_start, role_end, faction_start, faction_end,
                    survived, died_day, death_cause
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(game_id),
                        int(guild_id),
                        int(r["player_id"]),
                        r.get("role_start"),
                        r.get("role_end"),
                        r.get("faction_start"),
                        r.get("faction_end"),
                        int(r.get("survived", 0)),
                        int(r["died_day"]) if r.get("died_day") is not None else None,
                        r.get("death_cause"),
                    )
                    for r in game_player_rows
                ],
            )

            # player_stats upserts.
            for d in player_stats_deltas:
                conn.execute(
                    """
                    INSERT INTO player_stats(
                        guild_id, player_id,
                        games_played, wins_total, losses_total, draws_total,
                        wins_town, wins_mafia, wins_arsonist,
                        last_game_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, player_id) DO UPDATE SET
                        games_played = CASE WHEN (games_played + excluded.games_played) < 0 THEN 0 ELSE (games_played + excluded.games_played) END,
                        wins_total   = CASE WHEN (wins_total   + excluded.wins_total)   < 0 THEN 0 ELSE (wins_total   + excluded.wins_total)   END,
                        losses_total = CASE WHEN (losses_total + excluded.losses_total) < 0 THEN 0 ELSE (losses_total + excluded.losses_total) END,
                        draws_total  = CASE WHEN (draws_total  + excluded.draws_total)  < 0 THEN 0 ELSE (draws_total  + excluded.draws_total)  END,
                        wins_town    = CASE WHEN (wins_town    + excluded.wins_town)    < 0 THEN 0 ELSE (wins_town    + excluded.wins_town)    END,
                        wins_mafia   = CASE WHEN (wins_mafia   + excluded.wins_mafia)   < 0 THEN 0 ELSE (wins_mafia   + excluded.wins_mafia)   END,
                        wins_arsonist= CASE WHEN (wins_arsonist+ excluded.wins_arsonist)< 0 THEN 0 ELSE (wins_arsonist+ excluded.wins_arsonist) END,
                        last_game_at = COALESCE(excluded.last_game_at, player_stats.last_game_at)
                    """,
                    (
                        int(guild_id),
                        int(d["player_id"]),
                        int(d.get("games_played", 0)),
                        int(d.get("wins_total", 0)),
                        int(d.get("losses_total", 0)),
                        int(d.get("draws_total", 0)),
                        int(d.get("wins_town", 0)),
                        int(d.get("wins_mafia", 0)),
                        int(d.get("wins_arsonist", 0)),
                        d.get("last_game_at"),
                    ),
                )

            # player_role_stats upserts.
            for d in role_stats_deltas:
                conn.execute(
                    """
                    INSERT INTO player_role_stats(
                        guild_id, player_id, role,
                        played, wins_total, losses_total
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, player_id, role) DO UPDATE SET
                        played       = CASE WHEN (played       + excluded.played)       < 0 THEN 0 ELSE (played       + excluded.played)       END,
                        wins_total   = CASE WHEN (wins_total   + excluded.wins_total)   < 0 THEN 0 ELSE (wins_total   + excluded.wins_total)   END,
                        losses_total = CASE WHEN (losses_total + excluded.losses_total) < 0 THEN 0 ELSE (losses_total + excluded.losses_total) END
                    """,
                    (
                        int(guild_id),
                        int(d["player_id"]),
                        str(d["role"]),
                        int(d.get("played", 0)),
                        int(d.get("wins_total", 0)),
                        int(d.get("losses_total", 0)),
                    ),
                )

            # player_personal_stats upserts.
            for d in personal_win_deltas:
                conn.execute(
                    """
                    INSERT INTO player_personal_stats(guild_id, player_id, key, count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, player_id, key) DO UPDATE SET
                        count = CASE
                            WHEN (count + excluded.count) < 0 THEN 0
                            ELSE (count + excluded.count)
                        END
                    """,
                    (int(guild_id), int(d["player_id"]), str(d["key"]), int(d.get("delta", 0))),
                )

            return True, game_id

    # --------------------
    # Read helpers (leaderboard queries)
    # --------------------
    def get_player_stats_summary(self, *, guild_id: int, player_id: int) -> Optional[dict]:
        """
        Return a summary compatible with `!stats` rendering.

        Shape:
          {
            games_played, wins, losses, draws,
            role_played: {role: count},
            role_wins: {role: count},
            faction_played: {Town/Mafia/Neutral: count},
            faction_wins: {Town/Mafia/Arsonist: count},
            personal_wins: {Key: count},
          }
        """
        with self._transaction() as conn:
            st = conn.execute(
                """
                SELECT
                    MAX(games_played, 0) AS games_played,
                    MAX(wins_total, 0) AS wins_total,
                    MAX(losses_total, 0) AS losses_total,
                    MAX(draws_total, 0) AS draws_total,
                    MAX(wins_town, 0) AS wins_town,
                    MAX(wins_mafia, 0) AS wins_mafia,
                    MAX(wins_arsonist, 0) AS wins_arsonist
                FROM player_stats
                WHERE guild_id=? AND player_id=?
                """,
                (int(guild_id), int(player_id)),
            ).fetchone()
            if not st:
                return None

            role_rows = conn.execute(
                """
                SELECT role, played, wins_total
                FROM player_role_stats
                WHERE guild_id=? AND player_id=?
                """,
                (int(guild_id), int(player_id)),
            ).fetchall()

            # Faction played/wins can be computed from games history if desired, but we keep it simple:
            # - played: derived from role_played buckets
            # - wins: from stored wins_town/wins_mafia/wins_arsonist + (Neutral wins are personal/total)
            role_played: dict[str, int] = {}
            role_wins: dict[str, int] = {}
            faction_played: dict[str, int] = {"Town": 0, "Mafia": 0, "Neutral": 0}
            for r in role_rows:
                role = str(r["role"])
                played = int(r["played"])
                wins = int(r["wins_total"])
                role_played[role] = played
                role_wins[role] = wins
                if role in _MAFIA_ROLES_SET:
                    faction_played["Mafia"] += played
                elif role in _TOWN_ROLES_SET:
                    faction_played["Town"] += played
                else:
                    faction_played["Neutral"] += played

            personal_rows = conn.execute(
                """
                SELECT key, MAX(count, 0) AS count
                FROM player_personal_stats
                WHERE guild_id=? AND player_id=?
                """,
                (int(guild_id), int(player_id)),
            ).fetchall()
            personal = migrate_personal_wins_dict(
                {str(r["key"]): int(r["count"]) for r in personal_rows}
            )

            faction_wins = {
                "Town": int(st["wins_town"]),
                "Mafia": int(st["wins_mafia"]),
                "Arsonist": int(st["wins_arsonist"]),
            }

            return {
                "games_played": int(st["games_played"]),
                "wins": int(st["wins_total"]),
                "losses": int(st["losses_total"]),
                "draws": int(st["draws_total"]),
                "role_played": role_played,
                "role_wins": role_wins,
                "faction_played": faction_played,
                "faction_wins": faction_wins,
                "personal_wins": personal,
            }

    def build_json_players_mirror(self, *, guild_id: int) -> Dict[str, Dict]:
        """Rebuild JSON ``players`` dict from SQLite (canonical aggregates)."""
        gid = int(guild_id)
        with self._transaction() as conn:
            stat_rows = conn.execute(
                """
                SELECT player_id,
                       MAX(games_played, 0) AS games_played,
                       MAX(wins_total, 0) AS wins_total,
                       MAX(losses_total, 0) AS losses_total,
                       MAX(draws_total, 0) AS draws_total,
                       MAX(wins_town, 0) AS wins_town,
                       MAX(wins_mafia, 0) AS wins_mafia,
                       MAX(wins_arsonist, 0) AS wins_arsonist
                FROM player_stats
                WHERE guild_id=? AND games_played > 0
                ORDER BY player_id ASC
                """,
                (gid,),
            ).fetchall()
            if not stat_rows:
                return {}
            pids = [int(r["player_id"]) for r in stat_rows]
            placeholders = ",".join("?" for _ in pids)
            role_rows = conn.execute(
                f"""
                SELECT player_id, role, played, wins_total
                FROM player_role_stats
                WHERE guild_id=? AND player_id IN ({placeholders})
                """,
                (gid, *pids),
            ).fetchall()
            personal_rows = conn.execute(
                f"""
                SELECT player_id, key, MAX(count, 0) AS count
                FROM player_personal_stats
                WHERE guild_id=? AND player_id IN ({placeholders})
                """,
                (gid, *pids),
            ).fetchall()

        roles_by_pid: Dict[int, list] = {}
        for r in role_rows:
            roles_by_pid.setdefault(int(r["player_id"]), []).append(r)
        personal_by_pid: Dict[int, Dict[str, int]] = {}
        for r in personal_rows:
            personal_by_pid.setdefault(int(r["player_id"]), {})[str(r["key"])] = int(r["count"])

        players: dict[str, dict] = {}
        for st in stat_rows:
            pid = int(st["player_id"])
            role_played: dict[str, int] = {}
            role_wins: dict[str, int] = {}
            faction_played: dict[str, int] = {"Town": 0, "Mafia": 0, "Neutral": 0}
            for r in roles_by_pid.get(pid, []):
                role = str(r["role"])
                played = int(r["played"])
                wins = int(r["wins_total"])
                role_played[role] = played
                role_wins[role] = wins
                if role in _MAFIA_ROLES_SET:
                    faction_played["Mafia"] += played
                elif role in _TOWN_ROLES_SET:
                    faction_played["Town"] += played
                else:
                    faction_played["Neutral"] += played
            personal = migrate_personal_wins_dict(personal_by_pid.get(pid, {}))
            players[str(pid)] = {
                "games_played": int(st["games_played"]),
                "wins": int(st["wins_total"]),
                "losses": int(st["losses_total"]),
                "draws": int(st["draws_total"]),
                "role_played": role_played,
                "role_wins": role_wins,
                "faction_played": faction_played,
                "faction_wins": {
                    "Town": int(st["wins_town"]),
                    "Mafia": int(st["wins_mafia"]),
                    "Arsonist": int(st["wins_arsonist"]),
                },
                "personal_wins": personal,
            }
        return players

    def top_total_wins(self, *, guild_id: int, limit: int = 10) -> list[LeaderboardRow]:
        # Audit H3 — write paths clamp negatives via MAX(..., 0); read paths
        # must clamp too so leaderboards never surface negative counts.
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT player_id,
                       MAX(wins_total, 0) AS wins_total,
                       MAX(games_played, 0) AS games_played
                FROM player_stats
                WHERE guild_id=?
                ORDER BY MAX(wins_total, 0) DESC,
                         MAX(games_played, 0) DESC,
                         player_id ASC
                LIMIT ?
                """,
                (int(guild_id), int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins_total"]), int(r["games_played"]), int(r["wins_total"])) for r in rows]

    def top_faction_wins(self, *, guild_id: int, faction: str, limit: int = 10) -> list[LeaderboardRow]:
        col = {"Town": "wins_town", "Mafia": "wins_mafia", "Arsonist": "wins_arsonist"}.get(str(faction))
        if not col:
            raise ValueError(f"Unsupported faction: {faction}")
        # Audit H3 — clamp on read.
        with self._transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT player_id,
                       MAX({col}, 0) AS wins,
                       MAX(games_played, 0) AS games_played
                FROM player_stats
                WHERE guild_id=? AND MAX({col}, 0) > 0
                ORDER BY MAX({col}, 0) DESC,
                         MAX(games_played, 0) DESC,
                         player_id ASC
                LIMIT ?
                """,
                (int(guild_id), int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins"]), int(r["games_played"]), int(r["wins"])) for r in rows]

    def top_personal(self, *, guild_id: int, key: str, limit: int = 10) -> list[LeaderboardRow]:
        # Audit H3 — ORDER BY must use the same clamped expression as the
        # selected `wins` so a negative-stored row can't sort above clamped
        # zeros while being displayed as 0.
        keys = [str(key)]
        if str(key) == "guardian_angel_win":
            keys.append("guardian_angel_joint")
        placeholders = ",".join("?" for _ in keys)
        with self._transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    ps.player_id,
                    SUM(CASE WHEN ps.count > 0 THEN ps.count ELSE 0 END) AS wins,
                    MAX(COALESCE(st.games_played, 0)) AS games_played
                FROM player_personal_stats ps
                LEFT JOIN player_stats st
                    ON st.guild_id = ps.guild_id AND st.player_id = ps.player_id
                WHERE ps.guild_id=? AND ps.key IN ({placeholders})
                GROUP BY ps.player_id
                HAVING SUM(CASE WHEN ps.count > 0 THEN ps.count ELSE 0 END) > 0
                ORDER BY wins DESC,
                         games_played DESC,
                         ps.player_id ASC
                LIMIT ?
                """,
                (int(guild_id), *keys, int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins"]), int(r["games_played"]), int(r["wins"])) for r in rows]

    def top_winrate(self, *, guild_id: int, min_games: int = 5, limit: int = 10) -> list[LeaderboardRow]:
        # Audit H3 — clamp wins and games on read; winrate uses clamped values.
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT player_id,
                       MAX(wins_total, 0) AS wins_total,
                       MAX(games_played, 0) AS games_played
                FROM player_stats
                WHERE guild_id=? AND MAX(games_played, 0) >= ?
                ORDER BY (MAX(wins_total, 0) * 1.0 / NULLIF(MAX(games_played, 0), 0)) DESC,
                         MAX(games_played, 0) DESC,
                         player_id ASC
                LIMIT ?
                """,
                (int(guild_id), int(min_games), int(limit)),
            ).fetchall()
        out: list[LeaderboardRow] = []
        for r in rows:
            gp = int(r["games_played"])
            wins = int(r["wins_total"])
            out.append(LeaderboardRow(int(r["player_id"]), (wins / gp) if gp > 0 else 0.0, gp, wins))
        return out

    @staticmethod
    def _faction_for_role(role: str) -> str:
        if role in _TOWN_ROLES_SET:
            return "Town"
        if role in _MAFIA_ROLES_SET:
            return "Mafia"
        return "Neutral"

    def get_guild_stats_board(self, *, guild_id: int) -> Optional[dict]:
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT guild_id, channel_id, message_id, updated_at
                FROM guild_stats_board
                WHERE guild_id=?
                """,
                (int(guild_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "guild_id": int(row["guild_id"]),
            "channel_id": int(row["channel_id"]),
            "message_id": int(row["message_id"]),
            "updated_at": row["updated_at"],
        }

    def upsert_guild_stats_board(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        now = _utcnow_iso()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO guild_stats_board(guild_id, channel_id, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    updated_at = excluded.updated_at
                """,
                (int(guild_id), int(channel_id), int(message_id), now),
            )

    def delete_guild_stats_board(self, *, guild_id: int) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM guild_stats_board WHERE guild_id=?", (int(guild_id),))

    def list_guild_stats_boards(self) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT guild_id, channel_id, message_id, updated_at FROM guild_stats_board"
            ).fetchall()
        return [
            {
                "guild_id": int(r["guild_id"]),
                "channel_id": int(r["channel_id"]),
                "message_id": int(r["message_id"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def get_server_stats_summary(self, *, guild_id: int) -> dict:
        """
        Guild-wide aggregates (auto-updated on each endgame SQLite commit).

        Role/faction winrates sum ``player_role_stats`` across all players.
        Outcomes and death causes come from ``games`` / ``game_players`` history.
        """
        gid = int(guild_id)
        with self._transaction() as conn:
            games_completed = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM games WHERE guild_id=?",
                    (gid,),
                ).fetchone()["n"]
            )
            rostered_players = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM player_stats
                    WHERE guild_id=? AND games_played > 0
                    """,
                    (gid,),
                ).fetchone()["n"]
            )
            agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(games_played), 0) AS player_games,
                    COALESCE(SUM(wins_total), 0) AS wins,
                    COALESCE(SUM(losses_total), 0) AS losses,
                    COALESCE(SUM(draws_total), 0) AS draws
                FROM player_stats
                WHERE guild_id=?
                """,
                (gid,),
            ).fetchone()

            outcomes: list[dict[str, object]] = []
            for row in conn.execute(
                """
                SELECT outcome, COUNT(*) AS c
                FROM games
                WHERE guild_id=?
                GROUP BY outcome
                ORDER BY c DESC, outcome ASC
                """,
                (gid,),
            ):
                outcomes.append({"outcome": str(row["outcome"]), "count": int(row["c"])})

            if games_completed > 0:
                for o in outcomes:
                    o["pct"] = round(100.0 * int(o["count"]) / games_completed, 1)
            else:
                for o in outcomes:
                    o["pct"] = 0.0

            avg_row = conn.execute(
                """
                SELECT
                    AVG(ended_day_number) AS avg_days,
                    MIN(ended_day_number) AS min_days,
                    MAX(ended_day_number) AS max_days,
                    COUNT(*) AS n
                FROM games
                WHERE guild_id=? AND ended_day_number IS NOT NULL
                """,
                (gid,),
            ).fetchone()
            games_with_days = int(avg_row["n"] or 0)
            game_length: dict[str, object] = {
                "avg_days": round(float(avg_row["avg_days"]), 2) if avg_row["avg_days"] is not None else None,
                "min_days": int(avg_row["min_days"]) if avg_row["min_days"] is not None else None,
                "max_days": int(avg_row["max_days"]) if avg_row["max_days"] is not None else None,
                "games_with_days": games_with_days,
            }
            outcome_lengths: list[dict[str, object]] = []
            for row in conn.execute(
                """
                SELECT outcome, AVG(ended_day_number) AS avg_days, COUNT(*) AS c
                FROM games
                WHERE guild_id=? AND ended_day_number IS NOT NULL
                GROUP BY outcome
                ORDER BY c DESC, outcome ASC
                """,
                (gid,),
            ):
                outcome_lengths.append(
                    {
                        "outcome": str(row["outcome"]),
                        "avg_days": round(float(row["avg_days"]), 2),
                        "count": int(row["c"]),
                    }
                )
            game_length["by_outcome"] = outcome_lengths

            lobby_outcome_counts: dict[int, dict[str, int]] = {}
            for row in conn.execute(
                """
                SELECT player_count, outcome, COUNT(*) AS c
                FROM games
                WHERE guild_id=? AND player_count IS NOT NULL
                GROUP BY player_count, outcome
                """,
                (gid,),
            ):
                pc = int(row["player_count"])
                lobby_outcome_counts.setdefault(pc, {})[str(row["outcome"])] = int(row["c"])

            lobby_sizes: list[dict[str, object]] = []
            for row in conn.execute(
                """
                SELECT
                    player_count,
                    COUNT(*) AS games,
                    AVG(ended_day_number) AS avg_days
                FROM games
                WHERE guild_id=? AND player_count IS NOT NULL
                GROUP BY player_count
                ORDER BY player_count ASC
                """,
                (gid,),
            ):
                pc = int(row["player_count"])
                games_n = int(row["games"])
                oc_map = lobby_outcome_counts.get(pc, {})
                oc_rows = sorted(oc_map.items(), key=lambda kv: (-kv[1], kv[0]))
                outcome_breakdown = [
                    {
                        "outcome": name,
                        "count": cnt,
                        "pct": round(100.0 * cnt / games_n, 1) if games_n else 0.0,
                    }
                    for name, cnt in oc_rows
                ]
                lobby_sizes.append(
                    {
                        "player_count": pc,
                        "games": games_n,
                        "avg_days": round(float(row["avg_days"]), 2) if row["avg_days"] is not None else None,
                        "outcomes": outcome_breakdown,
                    }
                )

            roles: list[dict[str, object]] = []
            faction_totals: dict[str, dict[str, int]] = {
                "Town": {"played": 0, "wins": 0},
                "Mafia": {"played": 0, "wins": 0},
                "Neutral": {"played": 0, "wins": 0},
            }
            for row in conn.execute(
                """
                SELECT
                    role,
                    SUM(MAX(played, 0)) AS played,
                    SUM(MAX(wins_total, 0)) AS wins
                FROM player_role_stats
                WHERE guild_id=?
                GROUP BY role
                ORDER BY played DESC, role ASC
                """,
                (gid,),
            ):
                role = str(row["role"])
                played = int(row["played"])
                wins = int(row["wins"])
                if played <= 0:
                    continue
                roles.append({"role": role, "played": played, "wins": wins})
                fac = self._faction_for_role(role)
                faction_totals[fac]["played"] += played
                faction_totals[fac]["wins"] += wins

            factions = [
                {"faction": fac, "played": vals["played"], "wins": vals["wins"]}
                for fac, vals in faction_totals.items()
                if vals["played"] > 0
            ]
            factions.sort(key=lambda x: int(x["played"]), reverse=True)

            death_causes: list[dict[str, object]] = []
            for row in conn.execute(
                """
                SELECT death_cause, COUNT(*) AS c
                FROM game_players
                WHERE guild_id=?
                  AND death_cause IS NOT NULL
                  AND TRIM(death_cause) != ''
                GROUP BY death_cause
                ORDER BY c DESC, death_cause ASC
                LIMIT 20
                """,
                (gid,),
            ):
                death_causes.append(
                    {"cause": str(row["death_cause"]), "count": int(row["c"])}
                )

            personal_wins: list[dict[str, object]] = []
            raw_personal: dict[str, int] = {}
            for row in conn.execute(
                """
                SELECT key, SUM(CASE WHEN count > 0 THEN count ELSE 0 END) AS c
                FROM player_personal_stats
                WHERE guild_id=?
                GROUP BY key
                HAVING c > 0
                ORDER BY c DESC, key ASC
                """,
                (gid,),
            ):
                raw_personal[str(row["key"])] = int(row["c"])
            merged_personal = migrate_personal_wins_dict(raw_personal)
            for key, count in sorted(merged_personal.items(), key=lambda kv: (-kv[1], kv[0])):
                if count > 0:
                    personal_wins.append({"key": key, "count": int(count)})

        return {
            "games_completed": games_completed,
            "rostered_players": rostered_players,
            "player_games": int(agg["player_games"]),
            "wins": int(agg["wins"]),
            "losses": int(agg["losses"]),
            "draws": int(agg["draws"]),
            "outcomes": outcomes,
            "game_length": game_length,
            "lobby_sizes": lobby_sizes,
            "factions": factions,
            "roles": roles,
            "death_causes": death_causes,
            "personal_wins": personal_wins,
        }

    # --------------------
    # Import helper
    # --------------------
    def assess_import_staleness(self, *, guild_id: int, stats_data: dict, connection=None) -> Optional[str]:
        """
        Return a human-readable reason if SQLite looks newer than JSON (import would regress data).
        None means import is safe enough to proceed without force.
        """
        gid = int(guild_id)
        json_games = 0
        json_wins = 0
        players = (stats_data.get("players") or {}) if isinstance(stats_data, dict) else {}
        if isinstance(players, dict):
            for rec in players.values():
                if not isinstance(rec, dict):
                    continue
                try:
                    json_games = max(json_games, int(rec.get("games_played", 0) or 0))
                    json_wins = max(json_wins, int(rec.get("wins", 0) or 0))
                except (TypeError, ValueError):
                    continue
        with (nullcontext(connection) if connection is not None else self._transaction()) as conn:
            # Global maxima can hide a stale row for a newer or less active
            # player. Compare exactly the counters this import would replace.
            if isinstance(players, dict):
                for pid, rec in players.items():
                    if not isinstance(rec, dict):
                        continue
                    try:
                        uid = int(pid)
                        totals = {column: int(rec.get(key, 0)) for column, key in (
                            ('games_played', 'games_played'), ('wins_total', 'wins'),
                            ('losses_total', 'losses'), ('draws_total', 'draws'))}
                        factions = rec.get('faction_wins') if isinstance(rec.get('faction_wins'), dict) else {}
                        personal = migrate_personal_wins_dict(rec.get('personal_wins') if isinstance(rec.get('personal_wins'), dict) else {})
                        for column, key in (('wins_town', 'Town'), ('wins_mafia', 'Mafia'), ('wins_arsonist', 'Arsonist')):
                            totals[column] = max(0, int(factions.get(key, 0) or 0))
                        if not totals['wins_arsonist']:
                            totals['wins_arsonist'] = max(0, int(personal.get('arsonist_win', 0) or 0))
                    except (TypeError, ValueError):
                        continue  # The importer validates or skips malformed rows itself.
                    existing = conn.execute('SELECT * FROM player_stats WHERE guild_id=? AND player_id=?', (gid, uid)).fetchone()
                    stale = bool(existing and any(value < int(existing[column] or 0) for column, value in totals.items()))
                    for key, count in personal.items():
                        try:
                            value = max(0, int(count))
                        except (TypeError, ValueError):
                            continue
                        if value <= 0:
                            continue  # Zero/missing personal counters do not overwrite existing rows.
                        row = conn.execute('SELECT count FROM player_personal_stats WHERE guild_id=? AND player_id=? AND key=?', (gid, uid, str(key))).fetchone()
                        stale |= bool(row and value < int(row['count'] or 0))
                    played = rec.get('role_played') if isinstance(rec.get('role_played'), dict) else {}
                    wins = rec.get('role_wins') if isinstance(rec.get('role_wins'), dict) else {}
                    for role in set(played) | set(wins):
                        try:
                            rp, rw = max(0, int(played.get(role, 0) or 0)), max(0, int(wins.get(role, 0) or 0))
                        except (TypeError, ValueError):
                            continue
                        if rp <= 0 and rw <= 0:
                            continue
                        row = conn.execute('SELECT played, wins_total FROM player_role_stats WHERE guild_id=? AND player_id=? AND role=?', (gid, uid, str(role))).fetchone()
                        stale |= bool(row and (rp < int(row['played'] or 0) or rw < int(row['wins_total'] or 0)))
                    if stale:
                        return (f'SQLite has newer counters for player {uid}; the import was rejected without changes. '
                            'Use `!importstats force` only for an intentional replacement.')
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM games WHERE guild_id=?
                """,
                (gid,),
            ).fetchone()
            sqlite_games = int(row["n"] or 0) if row else 0
            agg = conn.execute(
                """
                SELECT COALESCE(MAX(games_played), 0) AS max_gp,
                       COALESCE(MAX(wins_total), 0) AS max_wins
                FROM player_stats WHERE guild_id=?
                """,
                (gid,),
            ).fetchone()
        sqlite_max_gp = int(agg["max_gp"] or 0) if agg else 0
        sqlite_max_wins = int(agg["max_wins"] or 0) if agg else 0
        if sqlite_games > 0 and sqlite_games > json_games:
            return (
                f"SQLite has **{sqlite_games}** completed games in history; "
                f"JSON player max games_played is **{json_games}**. "
                "Use `!importstats force` only if you intend to overwrite SQLite from JSON."
            )
        if sqlite_max_gp > json_games or sqlite_max_wins > json_wins:
            return (
                f"SQLite player totals look ahead of JSON (max games **{sqlite_max_gp}**, "
                f"max wins **{sqlite_max_wins}** vs JSON max games **{json_games}**). "
                "Use `!importstats force` to replace SQLite aggregates from JSON."
            )
        return None

    def import_player_stats_from_json(
        self,
        *,
        guild_id: int,
        stats_data: dict,
        all_or_nothing: bool = True,
        reject_stale: bool = False,
    ) -> int:
        """
        Import from the existing JSON stats structure into player_stats.
        Returns number of player records upserted.
        """
        players = (stats_data.get("players") or {}) if isinstance(stats_data, dict) else {}
        if not isinstance(players, dict):
            return 0
        now = _utcnow_iso()
        n = 0
        with self._transaction() as conn:
            if all_or_nothing or reject_stale:
                conn.execute("BEGIN IMMEDIATE")
            if reject_stale:
                reason = self.assess_import_staleness(guild_id=guild_id, stats_data=stats_data, connection=conn)
                if reason:
                    raise ValueError(reason)
            try:
                n = self._import_player_stats_from_json_unlocked(
                    conn,
                    guild_id=int(guild_id),
                    players=players,
                    now=now,
                )
                if all_or_nothing:
                    conn.commit()
            except Exception:
                if all_or_nothing:
                    conn.rollback()
                raise
        return n

    def _import_player_stats_from_json_unlocked(
        self,
        conn: Any,
        *,
        guild_id: int,
        players: dict,
        now: str,
    ) -> int:
        n = 0
        for pid_str, rec in players.items():
                try:
                    pid = int(pid_str)
                except (TypeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue

                try:
                    games = int(rec.get("games_played", 0))
                    wins = int(rec.get("wins", 0))
                    losses = int(rec.get("losses", 0))
                    draws = int(rec.get("draws", 0))
                except (TypeError, ValueError):
                    continue

                faction_wins_raw = rec.get("faction_wins") if isinstance(rec.get("faction_wins"), dict) else {}
                wins_town = max(0, int(faction_wins_raw.get("Town", 0) or 0))
                wins_mafia = max(0, int(faction_wins_raw.get("Mafia", 0) or 0))
                wins_arsonist = max(0, int(faction_wins_raw.get("Arsonist", 0) or 0))
                personal_raw = rec.get("personal_wins") if isinstance(rec.get("personal_wins"), dict) else {}
                personal_norm = migrate_personal_wins_dict(personal_raw)
                if wins_arsonist == 0:
                    wins_arsonist = max(0, int(personal_norm.get("arsonist_win", 0) or 0))

                conn.execute(
                    """
                    INSERT INTO player_stats(
                        guild_id, player_id,
                        games_played, wins_total, losses_total, draws_total,
                        wins_town, wins_mafia, wins_arsonist,
                        last_game_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, player_id) DO UPDATE SET
                        games_played = excluded.games_played,
                        wins_total = excluded.wins_total,
                        losses_total = excluded.losses_total,
                        draws_total = excluded.draws_total,
                        wins_town = excluded.wins_town,
                        wins_mafia = excluded.wins_mafia,
                        wins_arsonist = excluded.wins_arsonist,
                        last_game_at = excluded.last_game_at
                    """,
                    (
                        int(guild_id),
                        int(pid),
                        games,
                        wins,
                        losses,
                        draws,
                        wins_town,
                        wins_mafia,
                        wins_arsonist,
                        now,
                    ),
                )

                for key, count in personal_norm.items():
                    try:
                        delta = max(0, int(count))
                    except (TypeError, ValueError):
                        continue
                    if delta <= 0:
                        continue
                    conn.execute(
                        """
                        INSERT INTO player_personal_stats(guild_id, player_id, key, count)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id, player_id, key) DO UPDATE SET
                            count = excluded.count
                        """,
                        (int(guild_id), int(pid), str(key), delta),
                    )

                role_played = rec.get("role_played") if isinstance(rec.get("role_played"), dict) else {}
                role_wins = rec.get("role_wins") if isinstance(rec.get("role_wins"), dict) else {}
                roles = set(role_played.keys()) | set(role_wins.keys())
                for role in roles:
                    try:
                        played = max(0, int(role_played.get(role, 0) or 0))
                        rw = max(0, int(role_wins.get(role, 0) or 0))
                    except (TypeError, ValueError):
                        continue
                    if played <= 0 and rw <= 0:
                        continue
                    conn.execute(
                        """
                        INSERT INTO player_role_stats(
                            guild_id, player_id, role,
                            played, wins_total, losses_total
                        ) VALUES (?, ?, ?, ?, ?, 0)
                        ON CONFLICT(guild_id, player_id, role) DO UPDATE SET
                            played = excluded.played,
                            wins_total = excluded.wins_total
                        """,
                        (int(guild_id), int(pid), str(role), played, rw),
                    )
                n += 1
        return n

    # --------------------
    # DM outbox (B6.1) — durable player DMs, same SQLite file as stats/history
    # --------------------
    def enqueue_or_requeue_dm_outbox(
        self,
        *,
        guild_id: int,
        kind: str,
        dedupe_key: str,
        target_user_id: int,
        content: str,
        match_key: Optional[str] = None,
    ) -> Optional[int]:
        """Enqueue a DM; re-queue ``failed`` rows with the same dedupe key (e.g. GAME OVER)."""
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("enqueue_or_requeue_dm_outbox requires a non-empty dedupe_key")
        now = _utcnow_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id, status FROM dm_outbox WHERE guild_id=? AND dedupe_key=? LIMIT 1",
                (int(guild_id), key),
            ).fetchone()
            if row is not None:
                if str(row["status"]) == "failed":
                    conn.execute(
                        """UPDATE dm_outbox SET status='pending', attempts=0, last_error=NULL,
                           sending_since=NULL, not_before=?, content=?, target_user_id=?
                           WHERE id=?""",
                        (now, str(content), int(target_user_id), int(row["id"])),
                    )
                    return int(row["id"])
                return None
            cur = conn.execute(
                """
                INSERT INTO dm_outbox(
                    guild_id, kind, dedupe_key, match_key, target_user_id, content,
                    status, created_at, not_before
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (int(guild_id), str(kind), key, match_key, int(target_user_id), str(content), now, now),
            )
            return int(cur.lastrowid)

    def enqueue_dm_outbox(
        self,
        *,
        guild_id: int,
        kind: str,
        dedupe_key: str,
        target_user_id: int,
        content: str,
        match_key: Optional[str] = None,
    ) -> Optional[int]:
        """Enqueue a user DM. ``dedupe_key`` is required; duplicates are ignored (returns None)."""
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("enqueue_dm_outbox requires a non-empty dedupe_key")
        now = _utcnow_iso()
        with self._transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO dm_outbox(
                    guild_id, kind, dedupe_key, match_key, target_user_id, content,
                    status, created_at, not_before
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (int(guild_id), str(kind), key, match_key, int(target_user_id), str(content), now, now),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id FROM dm_outbox WHERE guild_id=? AND dedupe_key=? LIMIT 1",
                (int(guild_id), key),
            ).fetchone()
            return int(row["id"]) if row else None

    @staticmethod
    def _claim_dm_outbox_batch_fallback(
        conn: sqlite3.Connection, now_iso: str, lim: int
    ) -> tuple[list[sqlite3.Row], bool]:
        sel = conn.execute(
            """SELECT * FROM dm_outbox
               WHERE status='pending' AND (not_before IS NULL OR not_before<=?)
               ORDER BY id ASC LIMIT ?""",
            (now_iso, lim),
        ).fetchall()
        if not sel:
            return [], True
        ids = [int(r["id"]) for r in sel]
        placeholders = ",".join("?" * len(ids))
        upd = conn.execute(
            f"""UPDATE dm_outbox SET status='sending', sending_since=?
                WHERE id IN ({placeholders}) AND status='pending'""",
            (now_iso, *ids),
        )
        if upd.rowcount != len(ids):
            conn.rollback()
            return [], False
        rows = conn.execute(
            f"SELECT * FROM dm_outbox WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return rows, True

    def claim_dm_outbox_batch(self, *, limit: int = 25) -> list[dict]:
        now_iso = _utcnow_iso()
        lim = max(1, int(limit))
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows: list[sqlite3.Row]
            try:
                cur = conn.execute(
                    """UPDATE dm_outbox SET status='sending', sending_since=?
                       WHERE id IN (
                         SELECT id FROM (
                           SELECT id FROM dm_outbox
                           WHERE status='pending'
                             AND (not_before IS NULL OR not_before<=?)
                           ORDER BY id ASC
                           LIMIT ?
                         )
                       ) AND status='pending'
                       RETURNING *""",
                    (now_iso, now_iso, lim),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                conn.rollback()
                conn.execute("BEGIN IMMEDIATE")
                rows, ok = Database._claim_dm_outbox_batch_fallback(conn, now_iso, lim)
                if not ok:
                    return []
            conn.commit()
            return [dict(r) for r in rows]
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def mark_dm_outbox_sent(self, msg_id: int) -> None:
        now = _utcnow_iso()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE dm_outbox SET status='sent', sent_at=?, sending_since=NULL WHERE id=?",
                (now, int(msg_id)),
            )

    DM_OUTBOX_MAX_ATTEMPTS = 12

    def retry_dm_outbox_later(self, msg_id: int, *, error: str, delay_seconds: int) -> None:
        """HP03 — cap retries; mark terminal ``failed`` after DM_OUTBOX_MAX_ATTEMPTS."""
        delay_seconds = max(5, int(delay_seconds))
        nb = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=delay_seconds)
        nb_iso = nb.isoformat()
        max_attempts = self.DM_OUTBOX_MAX_ATTEMPTS
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE dm_outbox SET attempts = attempts + 1, last_error=?, sending_since=NULL
                   WHERE id=? RETURNING attempts""",
                (str(error)[:500], int(msg_id)),
            ).fetchone()
            attempts = int(row["attempts"]) if row else 1
            if attempts >= max_attempts:
                logging.warning(
                    "dm_outbox id=%s marked failed after %s attempts (guild may miss DM): %s",
                    int(msg_id),
                    attempts,
                    str(error)[:200],
                )
                conn.execute(
                    """UPDATE dm_outbox SET status='failed', sending_since=NULL WHERE id=?""",
                    (int(msg_id),),
                )
                return
            conn.execute(
                """UPDATE dm_outbox SET status='pending', not_before=? WHERE id=?""",
                (nb_iso, int(msg_id)),
            )

    def requeue_stale_dm_outbox_sending(self, *, stale_after_seconds: int = 300) -> int:
        cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=int(stale_after_seconds))
        cutoff_iso = cutoff.isoformat()
        with self._transaction() as conn:
            cur = conn.execute(
                """UPDATE dm_outbox SET status='pending', sending_since=NULL
                   WHERE status='sending' AND sending_since IS NOT NULL AND sending_since<=?""",
                (cutoff_iso,),
            )
            return int(cur.rowcount)

    def mark_dm_outbox_superseded(self, msg_id):
        with self._transaction() as conn:
            conn.execute("UPDATE dm_outbox SET status='superseded', sending_since=NULL WHERE id=?", (msg_id,))

    def defer_dm_outbox(self, msg_id, *, delay_seconds=12):
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        with self._transaction() as conn:
            conn.execute("UPDATE dm_outbox SET status='pending', sending_since=NULL, not_before=? WHERE id=?", (deadline,msg_id))

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


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

    def _conn(self) -> sqlite3.Connection:
        _ensure_parent_dir(self.path)
        # timeout avoids transient "database is locked" failures on Windows/slow disks
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        with self._conn() as conn:
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
                """
            )

            # Lightweight forward-only migrations (idempotent).
            migrations: list[tuple[str, str, str]] = [
                ("games", "ended_phase", "TEXT"),
                ("game_players", "death_cause", "TEXT"),
            ]
            for table, col, col_def in migrations:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    pass

        with self._conn() as conn:
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_dm_outbox_dedupe
                    ON dm_outbox(dedupe_key) WHERE dedupe_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_dm_outbox_pending
                    ON dm_outbox(status, not_before, id);

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('dm_outbox_schema', '1');
                """
            )

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
        with self._conn() as conn:
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

    def insert_game_players(
        self,
        *,
        game_id: int,
        guild_id: int,
        rows: Iterable[dict],
    ) -> None:
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
                if role in {"Mobster", "Framer", "Gravedigger", "Consort", "Hypnotist", "Mole", "Tailor", "Gatekeeper"}:
                    faction_played["Mafia"] += played
                elif role in {"Retributionist", "Vigilante", "Sheriff", "Investigator", "Doctor", "Escort", "Transporter", "Mayor", "Bodyguard", "Lookout", "Scary Grandma", "Tracker"}:
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
            personal = {str(r["key"]): int(r["count"]) for r in personal_rows}

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
    def top_total_wins(self, *, guild_id: int, limit: int = 10) -> list[LeaderboardRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT player_id, wins_total, games_played
                FROM player_stats
                WHERE guild_id=?
                ORDER BY wins_total DESC, games_played DESC, player_id ASC
                LIMIT ?
                """,
                (int(guild_id), int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins_total"]), int(r["games_played"]), int(r["wins_total"])) for r in rows]

    def top_faction_wins(self, *, guild_id: int, faction: str, limit: int = 10) -> list[LeaderboardRow]:
        col = {"Town": "wins_town", "Mafia": "wins_mafia", "Arsonist": "wins_arsonist"}.get(str(faction))
        if not col:
            raise ValueError(f"Unsupported faction: {faction}")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT player_id, {col} AS wins, games_played
                FROM player_stats
                WHERE guild_id=?
                ORDER BY wins DESC, games_played DESC, player_id ASC
                LIMIT ?
                """,
                (int(guild_id), int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins"]), int(r["games_played"]), int(r["wins"])) for r in rows]

    def top_personal(self, *, guild_id: int, key: str, limit: int = 10) -> list[LeaderboardRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    ps.player_id,
                    MAX(ps.count, 0) AS wins,
                    MAX(COALESCE(st.games_played, 0), 0) AS games_played
                FROM player_personal_stats ps
                LEFT JOIN player_stats st
                    ON st.guild_id = ps.guild_id AND st.player_id = ps.player_id
                WHERE ps.guild_id=? AND ps.key=?
                ORDER BY ps.count DESC, games_played DESC, ps.player_id ASC
                LIMIT ?
                """,
                (int(guild_id), str(key), int(limit)),
            ).fetchall()
        return [LeaderboardRow(int(r["player_id"]), float(r["wins"]), int(r["games_played"]), int(r["wins"])) for r in rows]

    def top_winrate(self, *, guild_id: int, min_games: int = 5, limit: int = 10) -> list[LeaderboardRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT player_id, wins_total, games_played
                FROM player_stats
                WHERE guild_id=? AND games_played >= ?
                ORDER BY (wins_total * 1.0 / NULLIF(games_played, 0)) DESC,
                         games_played DESC,
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

    # --------------------
    # Import helper
    # --------------------
    def import_player_stats_from_json(self, *, guild_id: int, stats_data: dict) -> int:
        """
        Import from the existing JSON stats structure into player_stats.
        Returns number of player records upserted.
        """
        players = (stats_data.get("players") or {}) if isinstance(stats_data, dict) else {}
        if not isinstance(players, dict):
            return 0
        now = _utcnow_iso()
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

            # Since the JSON stats already collapsed everything into wins/losses/draws,
            # we import the totals and leave faction/personal breakdown to accumulate going forward.
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO player_stats(
                        guild_id, player_id,
                        games_played, wins_total, losses_total, draws_total,
                        wins_town, wins_mafia, wins_arsonist,
                        last_game_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?)
                    ON CONFLICT(guild_id, player_id) DO UPDATE SET
                        games_played = excluded.games_played,
                        wins_total = excluded.wins_total,
                        losses_total = excluded.losses_total,
                        draws_total = excluded.draws_total,
                        last_game_at = excluded.last_game_at
                    """,
                    (int(guild_id), int(pid), games, wins, losses, draws, now),
                )
            n += 1
        return n

    # --------------------
    # DM outbox (B6.1) — durable player DMs, same SQLite file as stats/history
    # --------------------
    def enqueue_dm_outbox(
        self,
        *,
        guild_id: int,
        kind: str,
        dedupe_key: Optional[str],
        target_user_id: int,
        content: str,
    ) -> Optional[int]:
        """Enqueue a user DM. When dedupe_key is set, duplicates are ignored (returns None)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if dedupe_key:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO dm_outbox(
                        guild_id, kind, dedupe_key, target_user_id, content,
                        status, created_at, not_before
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (int(guild_id), str(kind), str(dedupe_key), int(target_user_id), str(content), now, now),
                )
                if cur.rowcount == 0:
                    return None
                row = conn.execute(
                    "SELECT id FROM dm_outbox WHERE dedupe_key=? LIMIT 1",
                    (str(dedupe_key),),
                ).fetchone()
                return int(row["id"]) if row else None
            cur = conn.execute(
                """
                INSERT INTO dm_outbox(
                    guild_id, kind, dedupe_key, target_user_id, content,
                    status, created_at, not_before
                ) VALUES (?, ?, NULL, ?, ?, 'pending', ?, ?)
                """,
                (int(guild_id), str(kind), int(target_user_id), str(content), now, now),
            )
            return int(cur.lastrowid)

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
        with self._conn() as conn:
            conn.execute(
                "UPDATE dm_outbox SET status='sent', sent_at=?, sending_since=NULL WHERE id=?",
                (now, int(msg_id)),
            )

    def retry_dm_outbox_later(self, msg_id: int, *, error: str, delay_seconds: int) -> None:
        delay_seconds = max(5, int(delay_seconds))
        nb = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=delay_seconds)
        nb_iso = nb.isoformat()
        with self._conn() as conn:
            conn.execute(
                """UPDATE dm_outbox SET status='pending', last_error=?, attempts=attempts+1,
                   sending_since=NULL, not_before=? WHERE id=?""",
                (str(error)[:500], nb_iso, int(msg_id)),
            )

    def requeue_stale_dm_outbox_sending(self, *, stale_after_seconds: int = 300) -> int:
        cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=int(stale_after_seconds))
        cutoff_iso = cutoff.isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE dm_outbox SET status='pending', sending_since=NULL
                   WHERE status='sending' AND sending_since IS NOT NULL AND sending_since<=?""",
                (cutoff_iso,),
            )
            return int(cur.rowcount)

"""Refresh the legacy export mirror without reapplying any match deltas."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from persistence import _save_stats_unlocked, guild_persist_lock, load_stats

if TYPE_CHECKING:
    from database import Database


def repair_guild_json_mirror_from_sqlite(db: Database, *, guild_id: int, game_key: str | None = None) -> bool:
    """Use the canonical SQLite aggregates and retain pending-recovery metadata."""
    try:
        with guild_persist_lock(guild_id):
            players = db.build_json_players_mirror(guild_id=guild_id)
            existing = load_stats(guild_id)
            payload = dict(existing) if isinstance(existing, dict) else {}
            meta = payload.get('_meta')
            meta = dict(meta) if isinstance(meta, dict) else {}
            if game_key is not None:
                meta['last_json_game_key'] = str(game_key)
            payload['players'] = players
            payload['_meta'] = meta
            # We already hold the non-reentrant guild lock. Reuse the atomic writer.
            _save_stats_unlocked(guild_id, payload)
        return True
    except Exception:
        logging.exception('Could not rebuild JSON stats mirror for guild %s', guild_id)
        return False

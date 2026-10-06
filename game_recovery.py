"""Endgame recovery helpers — commit pending stats before destructive disk ops."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from persistence import (
    clear_inline_pending_endgame_from_game_state,
    clear_pending_endgame_fallback,
    guild_persist_lock,
    is_stale_ended_state,
    load_pending_endgame_fallback,
    load_state,
    load_stats_meta,
    save_pending_endgame_fallback,
    save_stats_meta,
)


def _pending_endgame_meta(guild_id: int) -> Optional[Dict[str, Any]]:
    pending = load_stats_meta(guild_id).get("pending_endgame")
    if isinstance(pending, dict) and pending.get("outcome"):
        return pending
    fallback = load_pending_endgame_fallback(guild_id)
    if isinstance(fallback, dict) and fallback.get("outcome"):
        return fallback
    state_data = load_state(guild_id)
    if isinstance(state_data, dict):
        inline = state_data.get("_pending_endgame")
        if isinstance(inline, dict) and inline.get("outcome"):
            return inline
    return None


def persist_pending_endgame_marker(
    guild_id: int,
    *,
    pending: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Write pending endgame to stats meta; fallback file if meta write fails (#30)."""
    base = dict(meta if meta is not None else load_stats_meta(guild_id))
    base["pending_endgame"] = dict(pending)
    try:
        save_stats_meta(guild_id, base)
    except Exception:
        logging.exception(
            "save_stats_meta failed for pending_endgame; writing fallback guild_id=%s",
            guild_id,
        )
        try:
            save_pending_endgame_fallback(guild_id, pending)
        except Exception:
            logging.exception(
                "pending_endgame fallback write failed guild_id=%s",
                guild_id,
            )
            try:
                from persistence import embed_pending_endgame_in_game_state

                embed_pending_endgame_in_game_state(guild_id, pending)
            except Exception:
                logging.exception(
                    "pending_endgame inline game-state write failed guild_id=%s",
                    guild_id,
                )


def sqlite_has_game_key(game_key: str) -> Optional[bool]:
    """Return whether SQLite already has ``game_key``; ``None`` when DB unavailable."""
    pk = str(game_key or "").strip()
    if not pk:
        return None
    try:
        from game import try_get_bot

        bot = try_get_bot()
        db = getattr(bot, "db", None) if bot is not None else None
    except RuntimeError:
        db = None
    if db is None:
        return None
    try:
        hk = db.has_game_key(pk)
        return bool(hk) if isinstance(hk, bool) else False
    except Exception:
        logging.exception("SQLite game_key lookup failed game_key=%s", pk)
        return None


def clear_pending_endgame_meta(guild_id: int) -> None:
    """Drop pending endgame marker from stats meta, fallback, and inline game JSON (#41)."""
    from persistence import (
        _clear_inline_pending_endgame_unlocked,
        _clear_pending_endgame_fallback_unlocked,
        _save_stats_unlocked,
        load_stats,
    )

    with guild_persist_lock(guild_id):
        data = load_stats(guild_id) or {}
        meta = dict(data.get("_meta") or {}) if isinstance(data.get("_meta"), dict) else {}
        if "pending_endgame" in meta:
            meta.pop("pending_endgame", None)
            payload: Dict[str, Any] = {"_meta": meta}
            players = data.get("players")
            if isinstance(players, dict) and players:
                payload["players"] = players
            _save_stats_unlocked(guild_id, payload)
        _clear_pending_endgame_fallback_unlocked(guild_id)
        try:
            _clear_inline_pending_endgame_unlocked(guild_id)
        except Exception:
            logging.exception("Failed to clear inline pending_endgame guild_id=%s", guild_id)


def pending_endgame_matches_disk_state(guild_id: int) -> bool:
    pending = _pending_endgame_meta(guild_id)
    if not pending:
        return False
    pk = str(pending.get("game_key") or "").strip()
    state_data = load_state(guild_id)
    if not pk or not state_data:
        return False
    return str(state_data.get("game_key") or "").strip() == pk


def lobby_join_blocked_reason(guild_id: int) -> Optional[str]:
    """
    Return a user-facing reason when lobby writes must not clobber recovery state.

    Design 2A: block ``!join`` when disk shows deferred endgame / stale ended /
    matching ``pending_endgame``.
    """
    state_data = load_state(guild_id)
    if state_data and is_stale_ended_state(state_data):
        if state_data.get("cleanup_pending"):
            return (
                "🛑 The previous game ended without full cleanup (`cleanup_pending`). "
                "Run `!reset` or `!nukereset` before joining a new lobby."
            )
        if state_data.get("ending"):
            return (
                "🛑 The previous game ended without full cleanup. "
                "Run `!reset` or `!nukereset`, then `!startgame`."
            )
        return (
            "🛑 Stale ended game data is on disk. "
            "Run `!reset` or `!nukereset` before joining."
        )
    if pending_endgame_matches_disk_state(guild_id):
        return (
            "🛑 Endgame stats are still pending commit for the last game. "
            "Run `!reset` after stats recover, or wait for the bot to finish recovery."
        )
    pending = _pending_endgame_meta(guild_id)
    if pending and not state_data:
        return (
            "🛑 Endgame stats are still pending commit for the last game "
            "(game save already removed). Wait for cold-boot recovery or SQLite repair — "
            "do **not** run `!reset` until stats commit succeeds (reset can drop pending markers)."
        )
    return None


def disk_recovery_blocked_reason(guild_id: int) -> Optional[str]:
    """Shared recovery gate for ``!join``, ``!startgame``, and ``!importstats``."""
    return lobby_join_blocked_reason(guild_id)


def game_has_inflight_state(game: object) -> bool:
    """True when replacing the in-memory ``Game`` would orphan active coroutines (#13)."""
    from night_engine_checkpoint import resume_engine_in_progress

    return bool(
        getattr(game, "resolving", False)
        or getattr(game, "vote_in_progress", False)
        or getattr(game, "night_completion_snapshot", None)
        or resume_engine_in_progress(game)  # type: ignore[arg-type]
    )


def disk_recovery_summary(guild_id: int) -> Optional[str]:
    """Short operator hint for ``bothealth`` when memory placeholder hides disk truth."""
    state_data = load_state(guild_id)
    if not state_data:
        return None
    bits = []
    if state_data.get("cleanup_pending"):
        bits.append("cleanup_pending")
    if state_data.get("ending"):
        bits.append("ending")
    if is_stale_ended_state(state_data):
        bits.append("stale_ended")
    if _pending_endgame_meta(guild_id):
        bits.append("pending_endgame")
    if not bits:
        return None
    gk = str(state_data.get("game_key") or "").strip() or "?"
    return f"disk: {', '.join(bits)} (game_key={gk})"


def commit_pending_endgame_before_state_delete(guild_id: int) -> bool:
    """Design 1A — attempt stats commit while game JSON still exists."""
    from game import Game

    try:
        committed = Game.commit_pending_endgame_if_any(guild_id)
        if committed:
            logging.info(
                "Committed pending endgame stats before state delete guild_id=%s",
                guild_id,
            )
            clear_pending_endgame_meta(guild_id)
        return committed
    except Exception:
        logging.exception(
            "commit_pending_endgame_before_state_delete failed guild_id=%s",
            guild_id,
        )
        return False


def delete_game_state_locked(guild_id: int) -> None:
    """Delete game JSON under guild persist lock (#6)."""
    from persistence import _delete_state_unlocked

    with guild_persist_lock(guild_id):
        _delete_state_unlocked(guild_id)


def commit_and_maybe_delete_game_state(guild_id: int) -> bool:
    """
    Attempt pending stats commit, then delete game JSON only when no pending marker remains.
    """
    committed = commit_pending_endgame_before_state_delete(guild_id)
    with guild_persist_lock(guild_id):
        if _pending_endgame_meta(guild_id):
            logging.warning(
                "Retaining game JSON: pending endgame marker still present guild_id=%s committed=%s",
                guild_id,
                committed,
            )
            return committed
        from persistence import _delete_state_unlocked

        _delete_state_unlocked(guild_id)
    return committed


async def maybe_finish_deferred_cleanup(game: object, guild: object) -> bool:
    """
    Retry infrastructure reset when endgame deferred ``cleanup_pending`` and guild is available.
    """
    if not getattr(game, "cleanup_pending", False):
        return False
    if getattr(game, "in_progress", False):
        return False
    if getattr(game, "vote_in_progress", False):
        return False
    if getattr(game, "resolving", False):
        return False
    gid = int(getattr(game, "guild_id", 0) or 0)
    try:
        await asyncio.to_thread(commit_pending_endgame_before_state_delete, gid)
        reset = getattr(game, "reset", None)
        if reset is None:
            return False
        await reset(guild)  # type: ignore[misc]
        logging.info("Deferred cleanup reset completed guild_id=%s", gid)
        return True
    except Exception:
        logging.exception("Deferred cleanup reset failed guild_id=%s", gid)
        return False

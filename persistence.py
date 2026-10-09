import asyncio
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from copy import deepcopy
from weakref import WeakKeyDictionary
from typing import Any, Dict, Iterator, Optional


def _default_state_dir() -> Path:
    override = os.environ.get("MAFIABOT_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "state"


STATE_DIR = _default_state_dir()

_guild_io_guard = threading.Lock()
_guild_io_locks: Dict[int, threading.RLock] = {}
_legacy_db_migration_lock = threading.Lock()


def _guild_io_lock(guild_id: int) -> threading.RLock:
    gid = int(guild_id)
    with _guild_io_guard:
        lock = _guild_io_locks.get(gid)
        if lock is None:
            # Recovery and metadata transactions call readers while already
            # holding the same guild lock. Re-entry must remain on this thread.
            lock = threading.RLock()
            _guild_io_locks[gid] = lock
        return lock


@contextmanager
def guild_persist_lock(guild_id: int) -> Iterator[None]:
    """Serialize guild JSON reads, quarantine, and writes across ``Game`` instances."""
    lock = _guild_io_lock(guild_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def sqlite_db_path() -> Path:
    """Canonical SQLite path (same tree as game JSON / stats mirror)."""
    return STATE_DIR / "mafiabot.db"


def migrate_legacy_sqlite_db() -> None:
    """Snapshot legacy history, including its WAL, before creating the default DB."""
    root = Path(__file__).resolve().parent
    # An explicitly selected state tree is independent (including offline checks).
    if STATE_DIR.resolve() != (root / "state").resolve():
        return
    target = sqlite_db_path()
    legacy = root / "bot_app" / "state" / "mafiabot.db"
    with _legacy_db_migration_lock:
        try:
            target.stat()
        except FileNotFoundError:
            pass
        else:
            return
        try:
            legacy.stat()
        except FileNotFoundError:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _unique_tmp_for(target)
        try:
            # SQLite's backup reads committed WAL frames too. Copying just the
            # main file would silently drop history after an unclean shutdown.
            with closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(tmp)) as destination:
                    source.backup(destination)
            _replace_prepared_file(tmp, target)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                logging.exception("Could not remove interrupted legacy DB snapshot %s", tmp)
            raise
        logging.info("Migrated SQLite DB from %s to %s", legacy, target)


def _state_backup_max_per_guild() -> int:
    raw = os.environ.get("MAFIABOT_STATE_BACKUP_MAX", "20").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 20


def prune_old_state_backups(path: Path, *, max_backups: int | None = None) -> None:
    """Drop oldest ``{name}.bak.*`` siblings after each new backup (RC-11a)."""
    cap = _state_backup_max_per_guild() if max_backups is None else max(1, int(max_backups))
    pattern = f"{path.name}.bak.*"
    backups = sorted(
        path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[cap:]:
        try:
            old.unlink()
        except OSError:
            logging.exception("Failed to prune state backup %s", old)


def backup_file(path: Path) -> Optional[Path]:
    """Best-effort timestamped backup; returns backup path or None."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Several player actions can save within one clock tick. Every backup
    # needs its own path, including on filesystems with coarse timestamps.
    backup = path.with_name(f"{path.name}.bak.{stamp}.{uuid.uuid4().hex}")
    try:
        shutil.copy2(path, backup)
        prune_old_state_backups(path)
        return backup
    except Exception:
        logging.exception("Failed to backup %s", path)
        return None


def _state_path(guild_id: int) -> Path:
    return STATE_DIR / f"{guild_id}.json"

def _stats_path(guild_id: int) -> Path:
    return STATE_DIR / f"{guild_id}.stats.json"


def guild_stats_path(guild_id: int) -> Path:
    return _stats_path(guild_id)


def _unique_tmp_for(path: Path) -> Path:
    """Audit M1 — form a unique temp filename so concurrent flushes for
    the same guild (e.g., two asyncio.to_thread persist calls interleaving
    in the thread pool) can't clobber each other's tmp file mid-write."""
    nonce = f"{os.getpid()}.{uuid.uuid4().hex}"
    # Use .with_name so the suffix is the FULL final segment, not just the
    # last extension — `.json.tmp.{pid}.{uuid}` would otherwise lose the
    # original `.json` part when read back as path.suffix.
    return path.with_name(f"{path.name}.tmp.{nonce}")


def _replace_prepared_file(source: Path, target: Path) -> None:
    """Retry short Windows sharing locks without rewriting or deleting the save."""
    for delay in (0.01, 0.03, 0.1, 0.2, 0.4, None):
        try:
            source.replace(target)
            return
        except OSError as error:
            if delay is None or getattr(error, 'winerror', None) not in (5, 32, 33):
                raise
        # Async saves retain their worker and ordering/guild locks through this
        # wait. Cancellation must drain this same prepared replacement.
        time.sleep(delay)


class StateReadError(OSError):
    """Recovery is unavailable; callers must not replace the existing match."""


def _quarantine_corrupt(path: Path) -> None:
    """Preserve damaged bytes without replacing an earlier recovery copy.

    The caller holds the guild lock from the read through this rename, so a
    newer valid save cannot be quarantined in place of the bytes just read.
    """
    try:
        corrupt = path.with_name(f"{path.name}.corrupt")
        try:
            corrupt.stat()
        except FileNotFoundError:
            pass
        else:
            corrupt = path.with_name(f"{path.name}.corrupt.{uuid.uuid4().hex}")
        path.rename(corrupt)
    except OSError as error:
        raise StateReadError("Damaged saved data could not be preserved; retry recovery before replacing it.") from error


def load_state(guild_id: int) -> Optional[Dict[str, Any]]:
    with guild_persist_lock(guild_id):
        return _load_state_unlocked(guild_id)


def _load_state_unlocked(guild_id: int) -> Optional[Dict[str, Any]]:
    path = _state_path(guild_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Game snapshot must be an object")
        return data
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateReadError("Saved game is temporarily unreadable; retry recovery before starting another match.") from error
    except (ValueError, UnicodeError):
        logging.exception("Invalid persisted state in %s; quarantining damaged data.", str(path))
        _quarantine_corrupt(path)
        return None


def save_state(guild_id: int, data: Dict[str, Any]) -> None:
    with guild_persist_lock(guild_id):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(guild_id)
        existing = _load_state_unlocked(guild_id)
        payload = deepcopy(data)
        # This marker belongs to the recovery file, not to a detached Game
        # snapshot. Preserve additions made after that snapshot was prepared.
        pending = existing.get("_pending_endgame") if existing else None
        if isinstance(pending, dict) and pending.get("outcome"):
            payload["_pending_endgame"] = deepcopy(pending)
        if path.exists() and payload.get("in_progress"):
            backup_file(path)
        tmp = _unique_tmp_for(path)
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            _replace_prepared_file(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            raise


async def save_state_async(guild_id: int, data: Dict[str, Any]) -> None:
    await _save_async(_state_path(guild_id), save_state, guild_id, data)


def _delete_state_unlocked(guild_id: int) -> None:
    path = _state_path(guild_id)
    try:
        path.unlink(missing_ok=True)  # py3.8+ on Windows supports missing_ok
    except TypeError:
        if path.exists():
            path.unlink()


def embed_pending_endgame_in_game_state(guild_id: int, pending: Dict[str, Any]) -> None:
    """Last-resort pending marker when stats meta and fallback file writes both fail."""
    with guild_persist_lock(guild_id):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(guild_id)
        data = _load_state_unlocked(guild_id) or {}
        data["_pending_endgame"] = dict(pending)
        tmp = _unique_tmp_for(path)
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            _replace_prepared_file(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            raise


def _clear_inline_pending_endgame_unlocked(guild_id: int) -> None:
    path = _state_path(guild_id)
    data = _load_state_unlocked(guild_id)
    if not data or "_pending_endgame" not in data:
        return
    data.pop("_pending_endgame", None)
    tmp = _unique_tmp_for(path)
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _replace_prepared_file(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:
            pass
        raise


def clear_inline_pending_endgame_from_game_state(guild_id: int) -> None:
    with guild_persist_lock(guild_id):
        _clear_inline_pending_endgame_unlocked(guild_id)


def delete_state(guild_id: int) -> None:
    with guild_persist_lock(guild_id):
        _delete_state_unlocked(guild_id)


def is_stale_ended_state(data: Dict[str, Any]) -> bool:
    """True when persisted JSON is a ended game stub that needs GM reset before lobby."""
    if not isinstance(data, dict):
        return False
    if data.get("cleanup_pending"):
        return True
    if data.get("ending") and data.get("in_progress"):
        return True
    if data.get("ending") and not data.get("in_progress"):
        return True
    if not data.get("in_progress") and data.get("game_channel_id") and not (data.get("player_ids") or []):
        return True
    return False


def load_stats_meta(guild_id: int) -> Dict[str, Any]:
    """Load ``_meta`` from the stats JSON file (pending endgame markers only)."""
    data = load_stats(guild_id) or {}
    meta = data.get("_meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _pending_endgame_fallback_path(guild_id: int) -> Path:
    return STATE_DIR / f"{int(guild_id)}.pending_endgame.json"


def save_pending_endgame_fallback(guild_id: int, pending: Dict[str, Any]) -> None:
    """Durable pending-endgame marker when stats meta write fails (audit #30)."""
    with guild_persist_lock(guild_id):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _pending_endgame_fallback_path(guild_id)
        tmp = _unique_tmp_for(path)
        try:
            tmp.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_prepared_file(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            raise


def load_pending_endgame_fallback(guild_id: int) -> Optional[Dict[str, Any]]:
    with guild_persist_lock(guild_id):
        return _load_pending_endgame_fallback_unlocked(guild_id)


def _load_pending_endgame_fallback_unlocked(guild_id: int) -> Optional[Dict[str, Any]]:
    path = _pending_endgame_fallback_path(guild_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateReadError("Pending endgame recovery is temporarily unreadable; retry before starting another match.") from error
    except (ValueError, UnicodeError):
        logging.exception("Failed to load pending_endgame fallback guild_id=%s", guild_id)
        return None


def _clear_pending_endgame_fallback_unlocked(guild_id: int) -> None:
    try:
        _pending_endgame_fallback_path(guild_id).unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        p = _pending_endgame_fallback_path(guild_id)
        if p.exists():
            p.unlink()


def clear_pending_endgame_fallback(guild_id: int) -> None:
    with guild_persist_lock(guild_id):
        _clear_pending_endgame_fallback_unlocked(guild_id)


def save_stats_meta(guild_id: int, meta: Dict[str, Any]) -> None:
    """Persist ``_meta`` while preserving any existing ``players`` export snapshot."""
    clear_fallback = False
    with guild_persist_lock(guild_id):
        existing = load_stats(guild_id) or {}
        players = existing.get("players")
        payload: Dict[str, Any] = {"_meta": dict(meta)}
        if isinstance(players, dict) and players:
            payload["players"] = players
        _save_stats_unlocked(guild_id, payload)
        if not meta.get("pending_endgame"):
            _clear_pending_endgame_fallback_unlocked(guild_id)


def load_stats(guild_id: int) -> Optional[Dict[str, Any]]:
    with guild_persist_lock(guild_id):
        return _load_stats_unlocked(guild_id)


def _load_stats_unlocked(guild_id: int) -> Optional[Dict[str, Any]]:
    path = _stats_path(guild_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Statistics snapshot must be an object")
        return data
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateReadError("Saved statistics are temporarily unreadable; retry recovery before changing them.") from error
    except (ValueError, UnicodeError):
        logging.exception("Invalid statistics in %s; quarantining damaged data.", str(path))
        _quarantine_corrupt(path)
        return None


def _save_stats_unlocked(guild_id: int, data: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _stats_path(guild_id)
    tmp = _unique_tmp_for(path)
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _replace_prepared_file(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:
            pass
        raise


def save_stats(guild_id: int, data: Dict[str, Any]) -> None:
    with guild_persist_lock(guild_id):
        _save_stats_unlocked(guild_id, data)


async def save_stats_async(guild_id: int, data: Dict[str, Any]) -> None:
    await _save_async(_stats_path(guild_id), save_stats, guild_id, data)



_async_locks = WeakKeyDictionary()

async def _save_async(path: Path, writer, guild_id: int, data: Dict[str, Any]) -> None:
    # FIFO async locks preserve submission order, not worker scheduling order.
    # Keep locks local to the event loop so independent test/running loops work.
    locks = _async_locks.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault(path.resolve(), asyncio.Lock())
    snapshot = deepcopy(data)
    async with lock:
        worker = asyncio.create_task(asyncio.to_thread(writer, guild_id, snapshot))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop its OS thread. Do not let the
            # next writer overtake it, including during repeated cancellation.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise

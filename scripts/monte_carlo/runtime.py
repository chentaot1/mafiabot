"""MC batch runtime: quiet logging, reused asyncio loop, single-instance lock."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Loggers that emit per-trial noise during engine-backed sims.
_QUIET_LOGGER_NAMES = (
    "messages.delivery",
    "game",
    "root",
    "discord",
    "discord.client",
    "discord.gateway",
    "asyncio",
)

_loop: asyncio.AbstractEventLoop | None = None


def configure_quiet_logging(*, disable_below: int = logging.ERROR) -> None:
    """Suppress MC batch noise (warnings like post_game_channel unset)."""
    logging.basicConfig(level=disable_below, force=True)
    for name in _QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(disable_below)
    # Drop any handler noise below ERROR on the root logger.
    logging.disable(disable_below)


def run_async(coro: Any) -> Any:
    """
    Run one coroutine on a persistent loop.

    Replaces repeated asyncio.run() per night (which is slow on Windows and can
    exhaust resources over thousands of trials).
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)


def close_async_loop() -> None:
    """Release the MC asyncio loop (call at end of long batch scripts)."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None


def default_trial_workers(trials: int) -> int:
    """CPU workers for parallel generator trials (capped by trial count)."""
    cpu = os.cpu_count() or 4
    return max(1, min(cpu, max(1, int(trials))))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def batch_run_lock(lock_path: Path, *, purpose: str = "mc_batch") -> Iterator[None]:
    """
    Ensure only one long MC batch script runs at a time.

    Stale locks (dead PID) are cleared automatically.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            meta = json.loads(lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        other_pid = int(meta.get("pid", 0))
        if other_pid and other_pid != os.getpid() and _pid_alive(other_pid):
            started = meta.get("started", "?")
            raise RuntimeError(
                f"Another MC batch is already running (pid={other_pid}, started={started}). "
                f"Stop it before starting a new one. Lock: {lock_path}"
            )
        lock_path.unlink(missing_ok=True)

    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started": time.time(), "purpose": purpose}),
        encoding="utf-8",
    )
    try:
        yield
    finally:
        try:
            if lock_path.exists():
                meta = json.loads(lock_path.read_text(encoding="utf-8"))
                if int(meta.get("pid", 0)) == os.getpid():
                    lock_path.unlink(missing_ok=True)
        except OSError:
            pass

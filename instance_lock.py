"""Process-lifetime locks without sending signals to another process."""

import os
from pathlib import Path
from typing import BinaryIO


def acquire_instance_lock(path: Path) -> BinaryIO:
    """Return an open locked file; closing it (or exiting) releases the lock.

    Keep the file on disk: unlinking it would let another process lock a
    different file at the same path while the original lock is still held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            # Windows permits locking beyond EOF, including a new empty file.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise RuntimeError(f"Cannot acquire Mafia Bot instance lock: {path}") from error
    return handle

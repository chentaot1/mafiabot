"""Serialize match boundaries with private messages already in flight."""
import asyncio
from weakref import WeakKeyDictionary

_locks = WeakKeyDictionary()


def message_lock(guild_id):
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(('message', guild_id), asyncio.Lock())


def recovery_lock(guild_id):
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(('recovery', guild_id), asyncio.Lock())

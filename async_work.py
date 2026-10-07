"""Drain in-flight writes and requests before a lifecycle barrier is released."""
import asyncio
from functools import partial


async def run_blocking(function, /, *args, **kwargs):
    """Drain the OS worker before propagating cancellation or its write failure."""
    return await finish_pending(asyncio.to_thread(partial(function, *args, **kwargs)))


async def finish_pending(awaitable):
    """Cancellation cannot leave a mutating request running behind its owner."""
    worker = asyncio.ensure_future(awaitable)
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result

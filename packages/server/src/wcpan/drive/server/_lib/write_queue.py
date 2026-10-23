"""The write queue: one consumer serializes every SQLite write."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from logging import getLogger


_L = getLogger(__name__)

type Job = Callable[[], Awaitable[None]]
type WriteQueue = asyncio.Queue[Job]


def create_write_queue() -> WriteQueue:
    n = os.process_cpu_count() or 1
    return asyncio.Queue(maxsize=max(8, n * 2))


# Let-it-crash on write failure by design.
# Write failure in sqlite likely means the system is unstable.
# Discard any in-memory data, and let the next initialization recover the state.
async def write_worker(write_queue: WriteQueue) -> None:
    while True:
        task = await write_queue.get()
        try:
            await task()
        except Exception:
            _L.exception("write task failed")
            raise
        # Only a job that landed is done: a failed or interrupted one leaves
        # the queue unfinished, so ``join`` never reports a write that did
        # not happen. Waiters watch this task to stop waiting.
        write_queue.task_done()


async def run_queued[R](queue: WriteQueue, fn: Callable[[], Awaitable[R]]) -> R:
    """The result of *fn*, run by the queue's consumer after the jobs before it.

    A failure reaches the caller and the consumer alike. Calling this from a
    job of the same queue deadlocks.
    """
    done = asyncio.get_running_loop().create_future()

    async def job() -> None:
        try:
            result = await fn()
        except BaseException as e:
            if not done.done():
                done.set_exception(e)
            raise
        if not done.done():
            done.set_result(result)

    await queue.put(job)
    return await done

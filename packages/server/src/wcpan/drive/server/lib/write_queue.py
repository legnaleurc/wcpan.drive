import asyncio
from collections.abc import Awaitable, Callable


type Job = Callable[[], Awaitable[None]]


async def run_queued[R](queue: asyncio.Queue[Job], fn: Callable[[], Awaitable[R]]) -> R:
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

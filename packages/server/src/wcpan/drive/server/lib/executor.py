"""Executor calls whose result or side effects must not be lost to cancellation.

A call already running on a thread cannot be cancelled. These helpers make a
cancelled caller wait for it, so whatever the call touches stays valid until
it returns.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial


async def _settle[T](future: asyncio.Future[T]) -> None:
    """Wait for *future* to finish, through any number of cancellations."""
    while not future.done():
        try:
            await asyncio.wait([future])
        except asyncio.CancelledError:
            pass  # The caller is already cancelled and re-raises after this.


async def run_to_completion[**A, R](
    executor: Executor, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
) -> R:
    """Run *fn* on *executor*; a cancelled caller still waits for it to finish."""
    future = asyncio.get_running_loop().run_in_executor(
        executor, partial(fn, *args, **kwargs)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await _settle(future)
        raise


async def allocate[R](
    executor: Executor, fn: Callable[[], R], /, *, release: Callable[[R], None]
) -> R:
    """Run *fn* on *executor* and return what it allocated.

    If the caller is cancelled, waits for *fn* and passes its result to
    *release* instead, so the allocation does not leak.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, fn)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await _settle(future)
        if not future.cancelled() and future.exception() is None:
            await _settle(loop.run_in_executor(executor, release, future.result()))
        raise

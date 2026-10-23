import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial


_OFF_MAIN_TIMEOUT = 60.0  # seconds - crash if any DB op hangs this long


class OffMainService:
    def __init__(self, *, pool: Executor) -> None:
        self._pool = pool

    async def __call__[**A, R](
        self, fn: Callable[A, R], *args: A.args, **kwargs: A.kwargs
    ) -> R:
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(_OFF_MAIN_TIMEOUT):
            return await loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))

    async def untimed[**A, R](
        self, fn: Callable[A, R], *args: A.args, **kwargs: A.kwargs
    ) -> R:
        """For legitimately long-running work (e.g. file probing) that must not time out."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))

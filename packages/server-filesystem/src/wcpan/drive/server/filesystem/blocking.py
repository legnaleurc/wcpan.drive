"""Run blocking filesystem work on the backend's bounded thread pool."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial


class Blocking:
    """Bounded executor for directory listings, hashing, and file reads.

    Kept apart from the storage pool so bulk reads cannot starve database work.
    A running call cannot be cancelled; shutdown abandons queued calls only.
    """

    def __init__(self, *, executor: Executor) -> None:
        self._executor = executor

    async def __call__[**A, R](
        self, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
    ) -> R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

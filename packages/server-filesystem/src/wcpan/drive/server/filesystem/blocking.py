"""Run blocking filesystem work on the backend's bounded thread pool."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial

from wcpan.drive.server.lib.executor import allocate, run_to_completion


class Blocking:
    """Bounded executor for directory listings, hashing, and file reads.

    Kept apart from the mirror pool so bulk reads cannot starve database work.
    A running call cannot be cancelled; shutdown abandons queued calls only.
    """

    def __init__(self, *, executor: Executor) -> None:
        self._executor = executor

    async def __call__[**A, R](
        self, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
    ) -> R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

    async def to_completion[**A, R](
        self, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
    ) -> R:
        """Like calling, but a cancelled caller still waits for *fn* to finish.

        For calls that use a file the caller closes afterwards.
        """
        return await run_to_completion(self._executor, fn, *args, **kwargs)

    async def allocate[R](
        self, fn: Callable[[], R], /, *, release: Callable[[R], None]
    ) -> R:
        """Run *fn*, releasing what it returns if the caller is cancelled."""
        return await allocate(self._executor, fn, release=release)

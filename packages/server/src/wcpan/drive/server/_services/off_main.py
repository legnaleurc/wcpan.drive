import asyncio
import os
import threading
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial
from multiprocessing import parent_process


_OFF_MAIN_TIMEOUT = 60.0  # seconds - crash if any DB op hangs this long


def exit_with_server() -> None:
    """Pool initializer: end this worker when the server process is gone.

    Workers hold the pipes that would otherwise stop the forkserver and the
    resource tracker, so without this a killed server leaves them all running.
    A write in progress rolls back, as it does in any crash.
    """
    server = parent_process()
    assert server is not None

    def watch() -> None:
        server.join()
        os._exit(1)

    threading.Thread(target=watch, daemon=True, name="exit-with-server").start()


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

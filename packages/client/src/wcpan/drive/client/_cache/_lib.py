from collections.abc import Callable
from concurrent.futures import Executor
from typing import Concatenate, Pattern


class OffMainProcess:
    def __init__(self, *, dsn: str, pool: Executor) -> None:
        self._dsn = dsn
        self._pool = pool

    async def __call__[**A, R](
        self, fn: Callable[Concatenate[str, A], R], *args: A.args, **kwargs: A.kwargs
    ) -> R:
        from asyncio import get_running_loop
        from functools import partial

        bound = partial(fn, self._dsn, *args, **kwargs)
        loop = get_running_loop()
        return await loop.run_in_executor(self._pool, bound)


def sqlite3_regexp(_: str, cell: str | None, *, pattern: Pattern[str]) -> bool:
    if cell is None:
        # root node
        return False
    return pattern.search(cell) is not None

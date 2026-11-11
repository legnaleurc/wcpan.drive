from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import closing, contextmanager
from typing import Concatenate, Pattern

from wcpan.drive.sqlite import (
    read_only as read_connection,
)
from wcpan.drive.sqlite import (
    read_write as write_connection,
)


type RegexpFunction = Callable[..., bool]


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


@contextmanager
def read_only(
    dsn: str, *, timeout: float | None = None, regexp: RegexpFunction | None = None
):
    with read_connection(dsn, timeout=5.0 if timeout is None else timeout) as db:
        if regexp is not None:
            db.create_function("REGEXP", 2, regexp, deterministic=True)
        with closing(db.cursor()) as cursor:
            yield cursor


@contextmanager
def read_write(dsn: str, *, timeout: float | None = None):
    with (
        write_connection(dsn, timeout=5.0 if timeout is None else timeout) as db,
        closing(db.cursor()) as cursor,
    ):
        yield cursor


def sqlite3_regexp(_: str, cell: str | None, *, pattern: Pattern[str]) -> bool:
    if cell is None:
        # root node
        return False
    return pattern.search(cell) is not None

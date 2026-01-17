from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import closing, contextmanager
from re import Pattern
from sqlite3 import Row, connect
from typing import Concatenate


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
def connect_(dsn: str, *, timeout: float | None, regexp: RegexpFunction | None):
    if timeout is None:
        timeout = 5.0
    with connect(dsn, timeout=timeout) as db:
        db.row_factory = Row
        # FIXME error in the real world
        # await db.execute("PRAGMA foreign_keys = 1;")
        if regexp:
            db.create_function("REGEXP", 2, regexp, deterministic=True)
        yield db


@contextmanager
def read_only(
    dsn: str, *, timeout: float | None = None, regexp: RegexpFunction | None = None
):
    with (
        connect_(dsn, timeout=timeout, regexp=regexp) as db,
        closing(db.cursor()) as cursor,
    ):
        yield cursor


@contextmanager
def read_write(dsn: str, *, timeout: float | None = None):
    with (
        connect_(dsn, timeout=timeout, regexp=None) as db,
        closing(db.cursor()) as cursor,
    ):
        try:
            yield cursor
            if db.in_transaction:
                db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise


def sqlite3_regexp(_: str, cell: str | None, *, pattern: Pattern[str]) -> bool:
    if cell is None:
        # root node
        return False
    return pattern.search(cell) is not None

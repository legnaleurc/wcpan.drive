"""SQLite infrastructure; schemas and executor ownership belong to callers."""

import os
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from urllib.parse import quote


__all__ = (
    "connect",
    "read_only_dsn",
    "has_pending_wal",
    "uses_wal",
    "initialize",
    "read_only",
    "read_write",
)


@contextmanager
def connect(dsn: str, /, *, timeout: float = 5.0) -> Generator[sqlite3.Connection]:
    """Open an autocommit connection for setup or maintenance, then close it.

    *dsn* is a path, ``:memory:``, or a ``file:`` URI (``read_only_dsn``).
    """
    with closing(
        sqlite3.connect(dsn, timeout=timeout, autocommit=True, uri=True)
    ) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("PRAGMA synchronous = FULL")
        yield con


def read_only_dsn(path: str, /) -> str:
    """A DSN that opens the database at *path* without writing any file.

    ``immutable=1`` keeps SQLite from creating or updating the ``-wal`` and
    ``-shm`` files, which ``mode=ro`` alone still does. It also ignores the
    WAL and takes no locks, so it is correct only while nothing writes the
    database and ``has_pending_wal`` is false: committed transactions still in
    the WAL would not be seen.

    Symbolic links are resolved, as SQLite does to name the WAL files.
    """
    return f"file:{quote(os.path.realpath(path))}?mode=ro&immutable=1"


def has_pending_wal(path: str, /) -> bool:
    """True if the database at *path* has transactions only in its WAL.

    A zero-byte WAL, as a ``TRUNCATE`` checkpoint leaves, holds none. The WAL
    sits next to the database's real file: SQLite resolves symbolic links.
    """
    try:
        return os.path.getsize(f"{os.path.realpath(path)}-wal") > 0
    except FileNotFoundError:
        return False


def uses_wal(path: str, /) -> bool:
    """True if the database file at *path* is in WAL mode, from its header.

    An immutable connection cannot tell: it reports the rollback journal.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(20)
    except FileNotFoundError:
        return False
    # The file format's read and write versions are 2 in WAL mode.
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def initialize(dsn: str, /, *, timeout: float = 5.0) -> None:
    """Enable persistent WAL mode before schema setup and normal operations."""
    with connect(dsn, timeout=timeout) as con:
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(f"could not enable WAL mode: {mode}")


@contextmanager
def _transaction(
    con: sqlite3.Connection, *, write: bool
) -> Generator[sqlite3.Connection]:
    con.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


@contextmanager
def read_only(dsn: str, /, *, timeout: float = 5.0) -> Generator[sqlite3.Connection]:
    """Read a consistent snapshot; reject accidental writes."""
    with connect(dsn, timeout=timeout) as con:
        con.execute("PRAGMA query_only = ON")
        with _transaction(con, write=False):
            yield con


@contextmanager
def read_write(dsn: str, /, *, timeout: float = 5.0) -> Generator[sqlite3.Connection]:
    """Reserve the writer slot and commit or roll back the entire operation.

    Callers must not end the transaction. Scripts must not contain transaction
    commands; autocommit=True prevents executescript's legacy implicit commit.
    """
    with connect(dsn, timeout=timeout) as con, _transaction(con, write=True):
        yield con

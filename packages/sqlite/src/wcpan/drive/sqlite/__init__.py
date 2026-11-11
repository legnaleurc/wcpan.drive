"""SQLite infrastructure; schemas and executor ownership belong to callers."""

import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager


@contextmanager
def connect(dsn: str, /, *, timeout: float = 5.0) -> Generator[sqlite3.Connection]:
    """Open an autocommit connection for setup or maintenance, then close it."""
    with closing(sqlite3.connect(dsn, timeout=timeout, autocommit=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("PRAGMA synchronous = FULL")
        yield con


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


def checkpoint(dsn: str, /, *, timeout: float = 5.0) -> None:
    """Request WAL truncation outside a transaction; readers may prevent it."""
    with connect(dsn, timeout=timeout) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

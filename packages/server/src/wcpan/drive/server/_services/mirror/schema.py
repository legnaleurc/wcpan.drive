"""The core schema: DDL, compatibility changes, and the checks before opening."""

import re
import sqlite3
from contextlib import closing

from wcpan.drive.sqlite import (
    initialize as initialize_database,
)
from wcpan.drive.sqlite import (
    read_only as _read_only,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from .model import MirrorNotReadyError, SchemaVersionError, new_epoch, now_ms


DDL = """

CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT    PRIMARY KEY,
    parent_id    TEXT,
    name         TEXT    NOT NULL,
    is_directory INTEGER NOT NULL DEFAULT 0,
    created_time INTEGER NOT NULL,
    modified_time INTEGER NOT NULL,
    changed_time INTEGER NOT NULL,
    mime_type    TEXT    NOT NULL DEFAULT '',
    hash         TEXT    NOT NULL DEFAULT '',
    hash_algorithm TEXT  NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    is_image     INTEGER NOT NULL DEFAULT 0,
    is_video     INTEGER NOT NULL DEFAULT 0,
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_nodes_parent_id ON nodes(parent_id);

-- The server root is the only node without a parent; its ID is opaque.
CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_root ON nodes((parent_id IS NULL))
    WHERE parent_id IS NULL;

CREATE TABLE IF NOT EXISTS changes (
    change_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT    NOT NULL,
    is_removed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_changes_node_id ON changes(node_id);

-- Files withheld because their media probe failed and was never successful.
-- Keyed by the backend's own key for the file (``NodeObservation.source_key``),
-- so no public ID is kept for a file that was never published.
CREATE TABLE IF NOT EXISTS media_failures (
    -- Key of the backend instance that observed the file.
    instance  INTEGER NOT NULL,
    source_key TEXT   NOT NULL,
    -- Milliseconds since the epoch of the latest failure.
    marked_at INTEGER NOT NULL,
    -- Why the latest probe failed (``ProbeFailure``): transient, invalid, limit.
    kind      TEXT    NOT NULL DEFAULT 'invalid',
    PRIMARY KEY (instance, source_key)
);

-- Server-wide values. ``epoch`` names this change history: it is random when
-- the database is created and replaced by ``squash``, so cursors from another
-- history are recognized as stale.
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Backend instances seen by this database. ``key`` is assigned on first sight
-- and tags the instance's rows in backend tables and the ID map, so renaming an
-- instance means renaming ``name`` here and in the config together; renaming
-- only the config makes it a new instance. AUTOINCREMENT keeps a dropped
-- instance's key from being reused. ``schema`` names the backend tables
-- (``backend_schemas``) holding its rows, recorded when the instance first
-- registers them; NULL until then.
CREATE TABLE IF NOT EXISTS backend_instances (
    key    INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT    NOT NULL UNIQUE,
    schema TEXT
);

-- Version of each backend's tables (``BackendSchema.name``), shared by all of
-- its instances; recorded when the tables are first created.
CREATE TABLE IF NOT EXISTS backend_schemas (
    name    TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);

-- Public node ID of each native ID of a backend instance (by key). The root and
-- mount nodes belong to the core and have no row. Rows outlive their nodes, so
-- a late observation of a deleted node keeps its ID; offline gc prunes them.
CREATE TABLE IF NOT EXISTS node_ids (
    id       TEXT    PRIMARY KEY,
    instance INTEGER NOT NULL,
    native   TEXT NOT NULL,
    UNIQUE (instance, native)
);
"""

CORE_TABLES = frozenset(
    {
        "nodes",
        "changes",
        "media_failures",
        "metadata",
        "backend_instances",
        "backend_schemas",
        "node_ids",
    }
)
"""Tables owned by the core; every other table with an ``instance`` column is a
backend table."""

SCHEMA_VERSION = 1
"""Databases of other versions are not migrated: the server refuses them, and a
new one is built by initial discovery. Changes within a version are applied at
startup (``mirror._ensure_schema``)."""


def _migrate_pending_enrichment(con: sqlite3.Connection) -> None:
    """Replace ``pending_enrichment`` (keyed by public ID) with ``media_failures``.

    Records whose native ID differs from their public ID (Synology permanent
    links) carry over under that native ID. The others had the public ID as
    their native ID (the filesystem's in-memory IDs, which a rescan replaces),
    so they are dropped with the ID map rows they alone kept alive.
    """
    with closing(
        con.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type = 'table' AND name = 'pending_enrichment'"
        )
    ) as cur:
        if cur.fetchone() is None:
            return
    con.execute(
        """
        INSERT OR IGNORE INTO media_failures (instance, source_key, marked_at)
        SELECT i.instance, i.native, ? FROM pending_enrichment p
        JOIN node_ids i ON i.id = p.node_id
        WHERE i.native != i.id
        """,
        (now_ms(),),
    )
    con.execute(
        """
        DELETE FROM node_ids
        WHERE native = id
          AND id IN (SELECT node_id FROM pending_enrichment)
          AND id NOT IN (SELECT id FROM nodes)
        """
    )
    con.execute("DROP TABLE pending_enrichment")


def _drop_ms_duration(con: sqlite3.Connection) -> None:
    """Drop ``nodes.ms_duration``; the mirror no longer records durations."""
    with closing(con.execute("SELECT name FROM pragma_table_info('nodes')")) as cur:
        columns = {row[0] for row in cur}
    if "ms_duration" in columns:
        con.execute("ALTER TABLE nodes DROP COLUMN ms_duration")


def _add_media_failure_kind(con: sqlite3.Connection) -> None:
    """Add ``media_failures.kind``; earlier records count as invalid media."""
    with closing(
        con.execute("SELECT name FROM pragma_table_info('media_failures')")
    ) as cur:
        columns = {row[0] for row in cur}
    if "kind" not in columns:
        con.execute(
            "ALTER TABLE media_failures ADD COLUMN kind TEXT NOT NULL DEFAULT 'invalid'"
        )


def ensure_schema(dsn: str) -> str:
    """Create or check the schema, and return the change history's epoch."""
    initialize_database(dsn)
    with _read_write(dsn) as con:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        existing_tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        has_user_tables = bool(existing_tables - {"sqlite_sequence"})
        if version != SCHEMA_VERSION and (version != 0 or has_user_tables):
            raise SchemaVersionError(
                "database schema version mismatch: "
                f"expected {SCHEMA_VERSION}, got {version}. "
                "Older databases are not migrated: move this one aside and the "
                "server builds a new one by initial discovery (clients "
                "resynchronize)."
            )
        con.executescript(DDL)
        _add_media_failure_kind(con)
        _migrate_pending_enrichment(con)
        _drop_ms_duration(con)
        if version == 0:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute(
            "INSERT OR IGNORE INTO metadata (key, value) VALUES ('epoch', ?)",
            (new_epoch(),),
        )
        with closing(
            con.execute("SELECT value FROM metadata WHERE key = 'epoch'")
        ) as cur:
            return str(cur.fetchone()["value"])


# Indexes the core DDL creates; a read-only command cannot add a missing one.
_CORE_INDEXES = frozenset(
    re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", DDL)
)


_CHECKPOINT_HINT = (
    "run `wcpan.drive.server --config FILE checkpoint` with the server stopped"
)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    with closing(con.execute("SELECT name FROM pragma_table_info(?)", (table,))) as cur:
        return {str(row["name"]) for row in cur}


def check_schema(dsn: str) -> str:
    """Check the core schema without changing it; return the epoch.

    Refuses everything ``ensure_schema`` would still change, since a
    read-only command cannot make the change itself; the journal mode, which
    an immutable connection cannot report, is checked from the file header
    before opening (``uses_wal``).

    Raises:
        SchemaVersionError: For another schema version.
        MirrorNotReadyError: For a database the server has not set up, or one
            with pending compatibility changes.
    """
    with _read_only(dsn) as con:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        with closing(
            con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ) as cur:
            tables = {str(row["name"]) for row in cur}
        if version == 0 and not tables - {"sqlite_sequence"}:
            raise MirrorNotReadyError(
                "the database has no mirror yet; start the server to create it"
            )
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                "database schema version mismatch: "
                f"expected {SCHEMA_VERSION}, got {version}. "
                "Older databases are not migrated: move this one aside and the "
                "server builds a new one by initial discovery (clients "
                "resynchronize)."
            )
        missing = sorted(set(CORE_TABLES) - tables)
        if missing:
            raise MirrorNotReadyError(
                f"the database lacks tables {', '.join(missing)}; {_CHECKPOINT_HINT}"
            )
        with closing(
            con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        ) as cur:
            indexes = {str(row["name"]) for row in cur}
        missing = sorted(_CORE_INDEXES - indexes)
        if missing:
            raise MirrorNotReadyError(
                f"the database lacks indexes {', '.join(missing)}; {_CHECKPOINT_HINT}"
            )
        pending: list[str] = []
        if "kind" not in _columns(con, "media_failures"):
            pending.append("add media_failures.kind")
        if "pending_enrichment" in tables:
            pending.append("migrate pending_enrichment")
        if "ms_duration" in _columns(con, "nodes"):
            pending.append("drop nodes.ms_duration")
        with closing(
            con.execute("SELECT value FROM metadata WHERE key = 'epoch'")
        ) as cur:
            row = cur.fetchone()
        if row is None:
            pending.append("record the change history epoch")
        if pending or row is None:
            raise MirrorNotReadyError(
                f"the database needs changes ({'; '.join(pending)}); {_CHECKPOINT_HINT}"
            )
        return str(row["value"])

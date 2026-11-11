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
    height       INTEGER NOT NULL DEFAULT 0,
    ms_duration  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_nodes_parent_id ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS changes (
    change_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT    NOT NULL,
    is_removed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_changes_node_id ON changes(node_id);

-- Observations withheld because media enrichment failed. The node keeps its last
-- complete version (or stays unpublished) until a retry or newer observation.
CREATE TABLE IF NOT EXISTS pending_enrichment (
    node_id TEXT PRIMARY KEY,
    -- Backend instance whose observation was withheld.
    backend TEXT NOT NULL DEFAULT ''
);

-- Server-wide values. ``epoch`` names this change history: it is random when
-- the database is created and replaced by ``squash``, so cursors from another
-- history are recognized as stale.
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Backend instances seen by this database. ``key`` is assigned on first sight
-- and names the instance's tables and ID map rows; renaming an instance in the
-- config makes it a new one. ``schema_version`` is that of its tables, 0 until
-- they are created.
CREATE TABLE IF NOT EXISTS backends (
    name           TEXT PRIMARY KEY,
    key            TEXT NOT NULL UNIQUE,
    type           TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 0
);

-- Public node ID of each native ID of a backend instance (by key). The root and
-- mount nodes belong to the core and have no row. Rows outlive their nodes, so
-- a late observation of a deleted node keeps its ID; offline gc prunes them.
CREATE TABLE IF NOT EXISTS node_ids (
    id      TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    native  TEXT NOT NULL,
    UNIQUE (backend, native)
);
"""

SCHEMA_VERSION = 5
"""Version 5 mints public node IDs in the core (``node_ids``) and gives each
backend instance its own tables (``backends``). Older databases are not migrated:
the server refuses them, and a new one is built by initial discovery."""

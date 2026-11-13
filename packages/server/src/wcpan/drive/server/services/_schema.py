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
    node_id  TEXT PRIMARY KEY,
    -- Key of the backend instance whose observation was withheld; empty if
    -- the observation named none.
    instance TEXT NOT NULL DEFAULT ''
);

-- Server-wide values. ``epoch`` names this change history: it is random when
-- the database is created and replaced by ``squash``, so cursors from another
-- history are recognized as stale.
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Backend instances seen by this database. ``key`` is assigned on first sight
-- and tags the instance's rows in backend tables and the ID map; renaming an
-- instance in the config makes it a new one. ``schema`` names the backend
-- tables (``backend_schemas``) holding its rows, recorded when the instance
-- first registers them; NULL until then.
CREATE TABLE IF NOT EXISTS backend_instances (
    name   TEXT PRIMARY KEY,
    key    TEXT NOT NULL UNIQUE,
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
    id       TEXT PRIMARY KEY,
    instance TEXT NOT NULL,
    native   TEXT NOT NULL,
    UNIQUE (instance, native)
);
"""

CORE_TABLES = frozenset(
    {
        "nodes",
        "changes",
        "pending_enrichment",
        "metadata",
        "backend_instances",
        "backend_schemas",
        "node_ids",
    }
)
"""Tables owned by the core; every other table with an ``instance`` column is a
backend table."""

SCHEMA_VERSION = 5
"""Version 5 mints public node IDs in the core (``node_ids``) and registers
backend instances (``backend_instances``), whose rows backend tables tag by key. Older
databases are not migrated: the server refuses them, and a new one is built by
initial discovery."""

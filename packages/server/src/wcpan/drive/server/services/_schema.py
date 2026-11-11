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
"""

SCHEMA_VERSION = 4

# Version 4 adds per-node hash algorithms and moves the Synology locator out of
# the public node table into synology_locators, now owned by the Synology
# backend (``BackendSchema``). Before version 4 every stored digest came from
# Synology Drive. pending_enrichment is created by ``DDL``.
MIGRATE_3_TO_4 = """
ALTER TABLE nodes ADD COLUMN hash_algorithm TEXT NOT NULL DEFAULT '';
UPDATE nodes SET hash = '' WHERE is_directory = 1;
UPDATE nodes SET hash_algorithm = 'md4' WHERE is_directory = 0 AND hash != '';
CREATE TABLE synology_locators (
    node_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);
INSERT INTO synology_locators (node_id, file_id)
    SELECT id, mutable_id FROM nodes WHERE mutable_id != '';
DROP INDEX IF EXISTS ix_nodes_mutable_id;
ALTER TABLE nodes DROP COLUMN mutable_id;
"""

SCHEMA_NAME = "synology"

SCHEMA_VERSION = 1


DDL = """
-- Maps a public node to its mutable Synology file_id. Rows are removed together
-- with their node; they never emit changes. ``backend`` is the instance key.
CREATE TABLE IF NOT EXISTS synology_locators (
    node_id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    file_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_synology_locators_file_id
    ON synology_locators(backend, file_id);

-- Per-mount discovery checkpoint, invalidated when the source path changes.
CREATE TABLE IF NOT EXISTS synology_mounts (
    backend TEXT NOT NULL,
    name    TEXT NOT NULL,
    max_id  INTEGER NOT NULL,
    path    TEXT NOT NULL,
    PRIMARY KEY (backend, name)
);
"""

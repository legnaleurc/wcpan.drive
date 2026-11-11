DDL = """
-- Maps a public node to its mutable Synology file_id. Rows are removed together
-- with their node; they never emit changes.
CREATE TABLE IF NOT EXISTS synology_locators (
    node_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_synology_locators_file_id ON synology_locators(file_id);

-- Per-mount discovery checkpoint, invalidated when the source path changes.
CREATE TABLE IF NOT EXISTS mounts (
    name   TEXT PRIMARY KEY,
    max_id INTEGER NOT NULL,
    path   TEXT NOT NULL
);
"""

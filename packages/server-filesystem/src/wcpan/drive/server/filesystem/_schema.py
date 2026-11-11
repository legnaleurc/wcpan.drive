SCHEMA_NAME = "filesystem"

SCHEMA_VERSION = 1


DDL = """
-- ``backend`` is the instance key.
CREATE TABLE IF NOT EXISTS fs_entries (
    node_id  TEXT PRIMARY KEY,
    backend  TEXT NOT NULL,
    identity TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fs_entries_identity ON fs_entries(backend, identity);

CREATE TABLE IF NOT EXISTS fs_mounts (
    backend TEXT NOT NULL,
    name    TEXT NOT NULL,
    path    TEXT NOT NULL,
    fsid    TEXT NOT NULL,
    PRIMARY KEY (backend, name)
);
"""

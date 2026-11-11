DDL = """
CREATE TABLE IF NOT EXISTS fs_entries (
    node_id  TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fs_entries_identity ON fs_entries(identity);

CREATE TABLE IF NOT EXISTS fs_mounts (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fsid TEXT NOT NULL
);
"""

from wcpan.drive.server.services.mirror import BackendNamespace


SCHEMA_VERSION = 1


def ddl(ns: BackendNamespace) -> str:
    """Tables of one filesystem instance, named through its namespace."""
    entries = ns.table("fs_entries")
    return f"""
CREATE TABLE IF NOT EXISTS {entries} (
    node_id  TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS {ns.table("ix_fs_entries_identity")}
    ON {entries}(identity);

CREATE TABLE IF NOT EXISTS {ns.table("fs_mounts")} (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fsid TEXT NOT NULL
);
"""

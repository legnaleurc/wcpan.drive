from wcpan.drive.server.services.mirror import BackendNamespace


SCHEMA_VERSION = 1


def ddl(ns: BackendNamespace) -> str:
    """Tables of one Synology instance, named through its namespace."""
    locators = ns.table("synology_locators")
    mounts = ns.table("synology_mounts")
    return f"""
-- Maps a public node to its mutable Synology file_id. Rows are removed together
-- with their node; they never emit changes.
CREATE TABLE IF NOT EXISTS {locators} (
    node_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS {ns.table("ix_synology_locators_file_id")}
    ON {locators}(file_id);

-- Per-mount discovery checkpoint, invalidated when the source path changes.
CREATE TABLE IF NOT EXISTS {mounts} (
    name   TEXT PRIMARY KEY,
    max_id INTEGER NOT NULL,
    path   TEXT NOT NULL
);
"""

from typing import Literal, TypedDict


class JoinedDict(TypedDict):
    id: str
    name: str
    trashed: Literal[0, 1]
    created: int
    updated: int
    parent_id: str | None
    size: int | None
    hash: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    ms_duration: int | None
    extra: str | None


CURRENT_SCHEMA_VERSION = 5

SQL_CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS nodes (
        id TEXT NOT NULL,
        name TEXT,
        trashed BOOLEAN,
        created INTEGER,
        updated INTEGER,
        PRIMARY KEY (id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_nodes_names ON nodes(name);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_trashed ON nodes(trashed);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_created ON nodes(created);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_updated ON nodes(updated);",
    """
    CREATE TABLE IF NOT EXISTS files (
        id TEXT NOT NULL,
        mime_type TEXT,
        hash TEXT,
        size INTEGER,
        PRIMARY KEY (id),
        FOREIGN KEY (id) REFERENCES nodes (id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_files_mime_type ON files(mime_type);",
    """
    CREATE TABLE IF NOT EXISTS parents (
        id TEXT NOT NULL,
        parent_id TEXT NOT NULL,
        PRIMARY KEY (id, parent_id),
        FOREIGN KEY (id) REFERENCES nodes (id),
        FOREIGN KEY (parent_id) REFERENCES nodes (id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_parents_id ON parents(id);",
    "CREATE INDEX IF NOT EXISTS ix_parents_parent_id ON parents(parent_id);",
    """
    CREATE TABLE IF NOT EXISTS images (
        id TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY (id) REFERENCES nodes (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audios (
        id TEXT NOT NULL,
        ms_duration INTEGER NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY (id) REFERENCES nodes (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS extras (
        id TEXT NOT NULL,
        json JSON NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY (id) REFERENCES nodes (id)
    );
    """,
    f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};",
]

SQL_JOIN_TABLES = """
SELECT
    nodes.id AS id,
    nodes.name AS name,
    nodes.trashed AS trashed,
    nodes.created AS created,
    nodes.updated AS updated,
    parents.parent_id AS parent_id,
    files.mime_type AS mime_type,
    files.hash AS hash,
    files.size AS size,
    images.width AS width,
    images.height AS height,
    audios.ms_duration AS ms_duration,
    extras.json AS extra
FROM nodes
LEFT JOIN parents ON nodes.id = parents.id
LEFT JOIN files ON nodes.id = files.id
LEFT JOIN images ON nodes.id = images.id
LEFT JOIN audios ON nodes.id = audios.id
LEFT JOIN extras ON nodes.id = extras.id
"""
SQL_SELECT_NODE_BY_ID = SQL_JOIN_TABLES + "WHERE nodes.id = ?;"
SQL_SELECT_CHILD_BY_NAME = (
    SQL_JOIN_TABLES + "WHERE parents.parent_id = ? AND nodes.name = ?;"
)
SQL_SELECT_CHILDREN_BY_ID = SQL_JOIN_TABLES + "WHERE parents.parent_id = ?;"
SQL_SELECT_TRASHED_NODES = SQL_JOIN_TABLES + "WHERE nodes.trashed = ?;"
SQL_SELECT_NODES_BY_REGEX = SQL_JOIN_TABLES + "WHERE nodes.name REGEXP '';"
SQL_SELECT_ORPHAN_NODES = SQL_JOIN_TABLES + "WHERE parents.parent_id IS NULL;;"

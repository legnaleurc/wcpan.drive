CURRENT_SCHEMA_VERSION = 7

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
        created_time INTEGER,
        modified_time INTEGER,
        changed_time INTEGER,
        PRIMARY KEY (id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_nodes_names ON nodes(name);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_trashed ON nodes(trashed);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_created_time ON nodes(created_time);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_modified_time ON nodes(modified_time);",
    "CREATE INDEX IF NOT EXISTS ix_nodes_changed_time ON nodes(changed_time);",
    """
    CREATE TABLE IF NOT EXISTS files (
        id TEXT NOT NULL,
        mime_type TEXT,
        hash TEXT,
        hash_algorithm TEXT,
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

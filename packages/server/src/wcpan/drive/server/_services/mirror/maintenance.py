"""Offline and startup maintenance: gc, dropping or rebuilding an instance,
releasing reassigned mounts, checkpoint, and squash."""

import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.sqlite import (
    connect as _connect,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from .instances import instance_key
from .model import Instances, new_epoch
from .schema import CORE_TABLES, ensure_schema
from .writes import delete_subtree_on_connection


_GC_CREATE_SQL = """
    CREATE TEMP TABLE _dangling AS
    WITH RECURSIVE reachable(node_id, owner, is_root) AS (
        SELECT id, NULL, 1 FROM nodes WHERE parent_id IS NULL
        UNION ALL
        SELECT n.id,
               CASE WHEN r.is_root
                    THEN (SELECT o.key FROM temp._owners o WHERE o.name = n.name)
                    ELSE r.owner END,
               0
        FROM nodes n
        JOIN reachable r ON n.parent_id = r.node_id
        WHERE r.owner IS NULL
           OR EXISTS (
               SELECT 1 FROM node_ids i WHERE i.id = n.id AND i.instance = r.owner
           )
    )
    SELECT id AS node_id FROM nodes
    WHERE id NOT IN (SELECT node_id FROM reachable)
"""


def cleanup_dangling_nodes(dsn: str, *, mounts: Mapping[str, str] | None = None) -> int:
    """Delete unreachable nodes and emit removal changes; returns how many.

    A node is reachable from the server root through its parents. *mounts* maps
    each configured mount to the backend instance serving it: under those, only
    that instance's nodes are reachable, so a subtree left by a backend that no
    longer serves the mount is removed. Mounts not listed keep everything below
    them. Then ID map rows with no node are pruned.
    """
    ensure_schema(dsn)
    with _read_write(dsn) as con:
        con.execute("CREATE TEMP TABLE _owners (name TEXT PRIMARY KEY, key INTEGER)")
        # An instance never registered owns no node, so its mounts lose theirs:
        # keys start at 1, so 0 matches no row.
        con.executemany(
            """
            INSERT INTO temp._owners (name, key) VALUES
                (?, COALESCE((SELECT key FROM backend_instances WHERE name = ?), 0))
            """,
            list((mounts or {}).items()),
        )
        con.execute(_GC_CREATE_SQL)
        with closing(con.execute("SELECT COUNT(*) AS n FROM temp._dangling")) as cur:
            count = int(cur.fetchone()["n"])
        if count > 0:
            con.execute(
                "INSERT INTO changes (node_id, is_removed)"
                " SELECT node_id, 1 FROM temp._dangling"
            )
            con.execute(
                "DELETE FROM nodes WHERE id IN (SELECT node_id FROM temp._dangling)"
            )
        con.execute("DELETE FROM node_ids WHERE id NOT IN (SELECT id FROM nodes)")
        con.execute("DROP TABLE temp._dangling")
        con.execute("DROP TABLE temp._owners")
    return count


def _backend_tables(con: sqlite3.Connection) -> list[str]:
    """Backend tables: those outside the core with an ``instance`` column (see
    ``BackendSchema``)."""
    with closing(
        con.execute(
            """
            SELECT m.name FROM sqlite_master m
            WHERE m.type = 'table'
              AND EXISTS (
                SELECT 1 FROM pragma_table_info(m.name) WHERE name = 'instance'
              )
            ORDER BY m.name
            """
        )
    ) as cur:
        names = [str(row["name"]) for row in cur]
    return [n for n in names if n not in CORE_TABLES]


def _forget_instance(con: sqlite3.Connection, name: str, key: int) -> int:
    """Tombstone and delete the instance's nodes and its backend table rows.

    Returns the number of nodes removed. Clients do not cascade removals, so
    each node gets its own tombstone.
    """
    owned_sql = "SELECT id FROM node_ids WHERE instance = ?"
    with closing(
        con.execute(
            f"SELECT COUNT(*) AS n FROM nodes WHERE id IN ({owned_sql})", (key,)
        )
    ) as cur:
        count = int(cur.fetchone()["n"])
    con.execute(
        "INSERT INTO changes (node_id, is_removed)"
        f" SELECT id, 1 FROM nodes WHERE id IN ({owned_sql})",
        (key,),
    )
    con.execute(f"DELETE FROM nodes WHERE id IN ({owned_sql})", (key,))
    con.execute("DELETE FROM media_failures WHERE instance = ?", (key,))
    for table in _backend_tables(con):
        quoted = table.replace('"', '""')
        con.execute(f'DELETE FROM "{quoted}" WHERE instance = ?', (key,))
    return count


def drop_backend(dsn: str, name: str) -> int:
    """Remove instance *name*: its nodes (with tombstones), IDs, backend table
    rows, and registration, in one transaction. Returns the number of nodes removed.

    Raises:
        ValueError: If *name* is not registered.
    """
    ensure_schema(dsn)
    with _read_write(dsn) as con:
        key = instance_key(con, name)
        count = _forget_instance(con, name, key)
        con.execute("DELETE FROM node_ids WHERE instance = ?", (key,))
        con.execute("DELETE FROM backend_instances WHERE name = ?", (name,))
    return count


def rebuild_backend(dsn: str, name: str) -> int:
    """Remove instance *name*'s nodes (with tombstones) and backend table rows,
    keeping its registration and IDs, so initial discovery on the next start
    rebuilds it.

    Items whose native IDs are stable come back under their old public IDs; IDs
    left unused are pruned by a later ``gc``. Returns the number of nodes removed.

    Raises:
        ValueError: If *name* is not registered.
    """
    ensure_schema(dsn)
    with _read_write(dsn) as con:
        key = instance_key(con, name)
        return _forget_instance(con, name, key)


def release_reassigned_mounts(
    dsn: str, mounts: Mapping[NodeId, str], instances: Instances
) -> int:
    """Remove subtrees under each mount owned by another instance than its own.

    *mounts* maps mount node IDs to the instance now serving them. Returns the
    number of subtrees removed.
    """
    removed = 0
    with _read_write(dsn) as con:
        for mount, backend in mounts.items():
            with closing(
                con.execute(
                    """
                    SELECT n.id FROM nodes n JOIN node_ids i ON i.id = n.id
                    WHERE n.parent_id = ? AND i.instance IS NOT
                        (SELECT key FROM backend_instances WHERE name = ?)
                    """,
                    (str(mount), backend),
                )
            ) as cur:
                foreign = [NodeId(row["id"]) for row in cur]
            for node_id in foreign:
                delete_subtree_on_connection(con, node_id, instances)
            removed += len(foreign)
    return removed


def checkpoint_database(dsn: str, *, timeout: float = 5.0) -> None:
    """Apply the core schema's compatibility changes, then empty the WAL.

    What read-only maintenance commands need before they open the database,
    since they neither migrate nor read the WAL. Run with the server stopped.

    Raises:
        ValueError: If there is no database at *dsn*, or another connection
            keeps the WAL from being emptied.
    """
    if not os.path.exists(dsn):
        raise ValueError(f"no database at {dsn}; start the server to create it")
    ensure_schema(dsn)
    with _connect(dsn, timeout=timeout) as con:
        busy = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    if busy:
        raise ValueError(
            f"{dsn} is in use, so its WAL cannot be emptied; stop the server first"
        )


def reset_change_history(dsn: str) -> int:
    """Clear changes and insert one update row per node except the server root.

    Also replaces the epoch, so consumers holding a cursor from the old history
    are told to start over. Returns the number of change records inserted.
    """
    ensure_schema(dsn)
    with _read_write(dsn) as con:
        con.execute("UPDATE metadata SET value = ? WHERE key = 'epoch'", (new_epoch(),))
        con.execute("DELETE FROM changes")
        con.execute(
            "INSERT INTO changes (node_id, is_removed)"
            " SELECT id, 0 FROM nodes WHERE parent_id IS NOT NULL"
        )
        with closing(con.execute("SELECT COUNT(*) AS n FROM changes")) as cur:
            return int(cur.fetchone()["n"])

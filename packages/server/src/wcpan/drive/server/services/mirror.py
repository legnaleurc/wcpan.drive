import logging
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from typing import Concatenate, Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.sqlite import (
    checkpoint as _checkpoint,
)
from wcpan.drive.sqlite import (
    initialize as initialize_database,
)
from wcpan.drive.sqlite import (
    read_only as _read_only,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from ..lib.root import SERVER_ROOT_ID
from ..types import BackendLocator, NodeObservation
from ._schema import DDL, SCHEMA_VERSION
from .off_main import OffMainService


_L = logging.getLogger(__name__)


class SchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendNamespace:
    """Names of one backend instance's tables and indexes.

    Built only from the instance's registry key (``[0-9a-f]``), never from
    config strings, so the names are safe to interpolate into SQL.
    """

    key: str

    def table(self, name: str) -> str:
        """The instance's name for table or index *name*."""
        return f"{name}_{self.key}"


class BackendSchema(Protocol):
    """Backend-owned tables, maintained on the core's connection and transaction.

    Each backend instance gets its own tables, named through its namespace, so
    its lookups cannot see another instance's rows. Rows stay keyed by public
    node ID. Hooks must not commit, roll back, or open connections.
    """

    @property
    def version(self) -> int:
        """Version of the tables; a database with another version is refused."""
        ...

    def ensure_schema(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        """Create the instance's tables."""
        ...

    def bind_locator(
        self,
        con: sqlite3.Connection,
        ns: BackendNamespace,
        node_id: NodeId,
        locator: BackendLocator,
    ) -> None:
        """Persist the locator of *node_id* alongside its observation."""
        ...

    def forget_nodes(
        self, con: sqlite3.Connection, ns: BackendNamespace, node_ids: Sequence[str]
    ) -> None:
        """Drop backend state of removed nodes."""
        ...

    def forget_orphans(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        """Drop backend state of nodes no longer in ``nodes``."""
        ...


_MAX_CHANGES_PER_PAGE = 1000

_UPSERT_NODE_SQL = """
            INSERT INTO nodes
                (id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, hash_algorithm, size, is_image, is_video, width, height,
                 ms_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id,
                name=excluded.name,
                is_directory=excluded.is_directory,
                created_time=excluded.created_time,
                modified_time=excluded.modified_time,
                changed_time=excluded.changed_time,
                mime_type=excluded.mime_type,
                hash=excluded.hash,
                hash_algorithm=excluded.hash_algorithm,
                size=excluded.size,
                is_image=excluded.is_image,
                is_video=excluded.is_video,
                width=excluded.width,
                height=excluded.height,
                ms_duration=excluded.ms_duration
            """

# Variant used by scan and webhook paths: preserves existing non-zero width/height/ms_duration
# so that media info set by the API upload handler is not overwritten with zeros.
_PRESERVE_MEDIA_UPSERT_SQL = """
            INSERT INTO nodes
                (id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, hash_algorithm, size, is_image, is_video, width, height,
                 ms_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id,
                name=excluded.name,
                is_directory=excluded.is_directory,
                created_time=excluded.created_time,
                modified_time=excluded.modified_time,
                changed_time=excluded.changed_time,
                mime_type=excluded.mime_type,
                hash=excluded.hash,
                hash_algorithm=excluded.hash_algorithm,
                size=excluded.size,
                is_image=excluded.is_image,
                is_video=excluded.is_video,
                width=CASE WHEN excluded.width > 0 THEN excluded.width ELSE nodes.width END,
                height=CASE WHEN excluded.height > 0 THEN excluded.height ELSE nodes.height END,
                ms_duration=CASE WHEN excluded.ms_duration > 0 THEN excluded.ms_duration ELSE nodes.ms_duration END
            """


def _node_row_values(
    record: NodeRecord,
) -> tuple[
    str,
    str | None,
    str,
    int,
    int,
    int,
    int,
    str,
    str,
    str,
    int,
    int,
    int,
    int,
    int,
    int,
]:
    return (
        str(record.id),
        str(record.parent_id) if record.parent_id is not None else None,
        record.name,
        1 if record.is_directory else 0,
        record.created_time,
        record.modified_time,
        record.changed_time,
        record.mime_type,
        record.hash,
        record.hash_algorithm,
        record.size,
        1 if record.is_image else 0,
        1 if record.is_video else 0,
        record.width,
        record.height,
        record.ms_duration,
    )


@dataclass(frozen=True)
class _Instance:
    """A registered backend instance; *schema* is set once its factory registers it."""

    ns: BackendNamespace
    schema: BackendSchema | None = None


type _Instances = Mapping[str, _Instance]
"""Registered backend instances by name."""


def _with_schema(instances: _Instances) -> list[_Instance]:
    return [i for i in instances.values() if i.schema is not None]


def _instance_for(instances: _Instances, backend: str | None) -> _Instance | None:
    if backend is not None:
        return instances.get(backend)
    # An untagged observation is unambiguous with a single backend schema.
    candidates = _with_schema(instances)
    return candidates[0] if len(candidates) == 1 else None


def _new_epoch() -> str:
    return secrets.token_hex(8)


def _ensure_schema(dsn: str) -> str:
    """Create or check the schema, and return the change history's epoch."""
    initialize_database(dsn)
    with _read_write(dsn) as con:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        existing_tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        has_user_tables = bool(existing_tables - {"sqlite_sequence"})
        if version != SCHEMA_VERSION and (version != 0 or has_user_tables):
            raise SchemaVersionError(
                "database schema version mismatch: "
                f"expected {SCHEMA_VERSION}, got {version}. "
                "Older databases are not migrated: move this one aside and the "
                "server builds a new one by initial discovery (clients "
                "resynchronize)."
            )
        con.executescript(DDL)
        if version == 0:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute(
            "INSERT OR IGNORE INTO metadata (key, value) VALUES ('epoch', ?)",
            (_new_epoch(),),
        )
        with closing(
            con.execute("SELECT value FROM metadata WHERE key = 'epoch'")
        ) as cur:
            return str(cur.fetchone()["value"])


def _run_read[R](
    dsn: str,
    fn: Callable[..., R],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> R:
    with _read_only(dsn) as con:
        return fn(con, *args, **kwargs)


def _run_write[R](
    dsn: str,
    fn: Callable[..., R],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> R:
    with _read_write(dsn) as con:
        return fn(con, *args, **kwargs)


def _register_backend(dsn: str, name: str, type_: str) -> str:
    """The registry key of instance *name*, registering it on first sight.

    Raises:
        SchemaVersionError: If *name* was registered with another backend type.
    """
    with _read_write(dsn) as con:
        with closing(
            con.execute("SELECT key, type FROM backends WHERE name = ?", (name,))
        ) as cur:
            row = cur.fetchone()
        if row is not None:
            if type_ and row["type"] and row["type"] != type_:
                raise SchemaVersionError(
                    f"backend {name!r} was registered as {row['type']!r},"
                    f" not {type_!r}; a backend cannot change type"
                )
            if type_ and not row["type"]:
                con.execute(
                    "UPDATE backends SET type = ? WHERE name = ?", (type_, name)
                )
            return str(row["key"])
        while True:
            key = secrets.token_hex(4)
            cur = con.execute(
                "INSERT OR IGNORE INTO backends (name, key, type) VALUES (?, ?, ?)",
                (name, key, type_),
            )
            if cur.rowcount:
                return key


def _register_schema(
    dsn: str, name: str, ns: BackendNamespace, schema: BackendSchema
) -> None:
    """Create an instance's tables and drop rows whose nodes went away meanwhile.

    Offline ``gc`` runs without loading backends, so it cannot clean their rows.

    Raises:
        SchemaVersionError: If the instance's tables have another version.
    """
    with _read_write(dsn) as con:
        with closing(
            con.execute("SELECT schema_version FROM backends WHERE name = ?", (name,))
        ) as cur:
            version = int(cur.fetchone()["schema_version"])
        if version not in (0, schema.version):
            raise SchemaVersionError(
                f"backend {name!r} schema version mismatch: expected"
                f" {schema.version}, got {version}. Its tables are not migrated:"
                " move the database aside to rebuild it."
            )
        schema.ensure_schema(con, ns)
        if version == 0:
            con.execute(
                "UPDATE backends SET schema_version = ? WHERE name = ?",
                (schema.version, name),
            )
        schema.forget_orphans(con, ns)


def _owner_of(dsn: str, node_id: NodeId) -> str | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                """
                SELECT b.name FROM node_ids i JOIN backends b ON b.key = i.backend
                WHERE i.id = ?
                """,
                (str(node_id),),
            )
        ) as cur:
            row = cur.fetchone()
    return str(row["name"]) if row is not None else None


def _claim_id(con: sqlite3.Connection, node_id: NodeId, instance: _Instance) -> None:
    """Give a node minted by its backend (``NodeIdMap.mint``) its map row."""
    con.execute(
        "INSERT OR IGNORE INTO node_ids (id, backend, native) VALUES (?, ?, ?)",
        (str(node_id), instance.ns.key, str(node_id)),
    )


def _upsert_node_and_emit_change(dsn: str, record: NodeRecord) -> None:
    with _read_write(dsn) as con:
        con.execute(_PRESERVE_MEDIA_UPSERT_SQL, _node_row_values(record))
        con.execute(
            "INSERT INTO changes (node_id, is_removed) VALUES (?, 0)",
            (str(record.id),),
        )


def _delete_subtree_on_connection(
    con: sqlite3.Connection,
    node_id: NodeId,
    instances: _Instances,
) -> None:
    """Delete node and all descendants; emit remove-changes for each.

    Also drops pending enrichment for them, including *node_id* when it was
    never published.
    """
    con.execute("DELETE FROM pending_enrichment WHERE node_id = ?", (str(node_id),))
    # Find all descendants including the root node_id using a recursive CTE.
    sql = """
    WITH RECURSIVE descendants(node_id) AS (
        SELECT ?
        UNION ALL
        SELECT n.id FROM nodes n
        JOIN descendants d ON n.parent_id = d.node_id
    )
    SELECT node_id FROM descendants
    """
    with closing(con.execute(sql, (str(node_id),))) as cur:
        to_delete = [row["node_id"] for row in cur]

    if not to_delete:
        return

    # Delete from nodes and insert into changes in bulk.
    placeholders = ",".join("?" * len(to_delete))
    con.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", to_delete)
    for instance in _with_schema(instances):
        assert instance.schema is not None
        instance.schema.forget_nodes(con, instance.ns, to_delete)
    con.execute(
        f"DELETE FROM pending_enrichment WHERE node_id IN ({placeholders})", to_delete
    )
    con.executemany(
        "INSERT INTO changes (node_id, is_removed) VALUES (?, 1)",
        [(nid,) for nid in to_delete],
    )


def _delete_subtree_and_emit_changes(
    dsn: str, node_id: NodeId, instances: _Instances
) -> None:
    with _read_write(dsn) as con:
        _delete_subtree_on_connection(con, node_id, instances)


def _node_exists(con: sqlite3.Connection, node_id: NodeId) -> bool:
    with closing(
        con.execute("SELECT 1 FROM nodes WHERE id = ?", (str(node_id),))
    ) as cur:
        return cur.fetchone() is not None


def _select_node_row(
    con: sqlite3.Connection, node_id: NodeId
) -> tuple[object, ...] | None:
    with closing(
        con.execute("SELECT * FROM nodes WHERE id = ?", (str(node_id),))
    ) as cur:
        row = cur.fetchone()
    return tuple(row) if row is not None else None


def _apply_observations(
    dsn: str, observations: list[NodeObservation], instances: _Instances
) -> set[NodeId]:
    """Upsert nodes and their locators in order, skipping any whose parent is gone.

    The parent check and writes share one explicit transaction, so an observation
    made before its parent was removed cannot recreate an orphan. A change is
    emitted only when the public node row actually changes; locator-only updates
    are silent. An applied observation is complete, so it clears any pending
    enrichment for the node. Returns the IDs of the nodes that were applied.
    """
    if not observations:
        return set()
    applied: set[NodeId] = set()
    with _read_write(dsn) as con:
        for observation in observations:
            record = observation.record
            if record.parent_id is not None and not _node_exists(con, record.parent_id):
                continue
            before = _select_node_row(con, record.id)
            con.execute(_PRESERVE_MEDIA_UPSERT_SQL, _node_row_values(record))
            if _select_node_row(con, record.id) != before:
                con.execute(
                    "INSERT INTO changes (node_id, is_removed) VALUES (?, 0)",
                    (str(record.id),),
                )
            if observation.backend is not None:
                owner = instances.get(observation.backend)
                if owner is not None:
                    _claim_id(con, record.id, owner)
            instance = _instance_for(instances, observation.backend)
            if (
                observation.locator is not None
                and instance is not None
                and instance.schema is not None
            ):
                instance.schema.bind_locator(
                    con, instance.ns, record.id, observation.locator
                )
            con.execute(
                "DELETE FROM pending_enrichment WHERE node_id = ?", (str(record.id),)
            )
            applied.add(record.id)
    return applied


def _mark_enrichment_pending(
    dsn: str, node_id: NodeId, backend: str | None, instances: _Instances
) -> None:
    with _read_write(dsn) as con:
        # A withheld node keeps its ID, and stays routable, before it is published.
        owner = instances.get(backend) if backend is not None else None
        if owner is not None:
            _claim_id(con, node_id, owner)
        con.execute(
            """
            INSERT INTO pending_enrichment (node_id, backend) VALUES (?, ?)
            ON CONFLICT(node_id) DO UPDATE SET backend = excluded.backend
            """,
            (str(node_id), backend or ""),
        )


def _get_pending_enrichment(dsn: str, backend: str | None) -> list[NodeId]:
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                "SELECT node_id FROM pending_enrichment WHERE backend = ?"
                " ORDER BY node_id",
                (backend or "",),
            )
        ) as cur:
            return [NodeId(row["node_id"]) for row in cur.fetchall()]


def _bulk_upsert_nodes(dsn: str, records: list[NodeRecord]) -> None:
    """Upsert nodes only (no change rows) in one transaction."""
    if not records:
        return
    with _read_write(dsn) as con:
        con.executemany(_UPSERT_NODE_SQL, [_node_row_values(r) for r in records])


def _get_node_by_id(dsn: str, node_id: NodeId) -> NodeRecord | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE id = ?", (str(node_id),))
        ) as cur:
            row = cur.fetchone()
    return row_to_record(row) if row else None


def _get_nodes_by_ids(
    dsn: str,
    node_ids: list[NodeId],
) -> dict[NodeId, NodeRecord]:
    if not node_ids:
        return {}
    with _read_only(dsn) as con:
        placeholders = ",".join("?" * len(node_ids))
        with closing(
            con.execute(
                f"SELECT * FROM nodes WHERE id IN ({placeholders})",
                [str(_) for _ in node_ids],
            )
        ) as cur:
            rows = cur.fetchall()
    return {NodeId(row["id"]): row_to_record(row) for row in rows}


def _get_children(dsn: str, parent_id: NodeId) -> list[NodeRecord]:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE parent_id = ?", (str(parent_id),))
        ) as cur:
            rows = cur.fetchall()
    return [row_to_record(r) for r in rows]


def _collect_subtree_node_ids(dsn: str, root_id: NodeId) -> set[NodeId]:
    """All node_id values in the DB subtree rooted at *root_id* (including *root_id*)."""
    sql = """
    WITH RECURSIVE descendants(node_id) AS (
        SELECT ?
        UNION ALL
        SELECT n.id FROM nodes n
        JOIN descendants d ON n.parent_id = d.node_id
    )
    SELECT node_id FROM descendants
    """
    with _read_only(dsn) as con:
        with closing(con.execute(sql, (str(root_id),))) as cur:
            return {NodeId(row["node_id"]) for row in cur}


def _get_ancestors(dsn: str, node_id: NodeId) -> list[NodeRecord]:
    """Fetch the ancestor chain starting from *node_id*, stopping before the mount.

    Returns all nodes from *node_id* up to (but not including) the first
    structural ancestor: a mount node (a child of the root) or the root itself.
    Single query via recursive CTE.
    """
    sql = """
    WITH RECURSIVE ancestors AS (
        SELECT * FROM nodes WHERE id = ?
        UNION ALL
        SELECT n.* FROM nodes n
        JOIN ancestors a ON n.id = a.parent_id
        WHERE n.parent_id IS NOT NULL AND n.parent_id != ?
    )
    SELECT * FROM ancestors
    """
    with _read_only(dsn) as con:
        with closing(con.execute(sql, (str(node_id), str(SERVER_ROOT_ID)))) as cur:
            return [row_to_record(r) for r in cur.fetchall()]


def _deferred_removal_roots(
    dsn: str,
    preserved: set[NodeId],
    scope: set[NodeId],
) -> list[NodeId]:
    """Roots of the subtrees under *scope* whose nodes are not in *preserved*.

    The *scope* nodes themselves are never roots.
    """
    if not scope:
        return []

    placeholders = ",".join("?" * len(scope))
    # Collect all descendants of the scope nodes except mount nodes, together
    # with their parent_ids, so we can compute deletion roots in one transaction.
    sql = f"""
    WITH RECURSIVE descendants(node_id, parent_id) AS (
        SELECT id, parent_id FROM nodes WHERE parent_id IN ({placeholders})
        UNION ALL
        SELECT n.id, n.parent_id FROM nodes n
        JOIN descendants d ON n.parent_id = d.node_id
    )
    SELECT node_id, parent_id FROM descendants WHERE parent_id != ?
    """
    params = tuple(str(_) for _ in scope) + (str(SERVER_ROOT_ID),)
    with _read_only(dsn) as con:
        with closing(con.execute(sql, params)) as cur:
            rows = cur.fetchall()

    parent_by_id: dict[NodeId, NodeId | None] = {
        NodeId(row["node_id"]): (
            NodeId(row["parent_id"]) if row["parent_id"] is not None else None
        )
        for row in rows
    }
    candidates = {nid for nid in parent_by_id if nid not in preserved}
    return sorted(nid for nid in candidates if parent_by_id.get(nid) not in candidates)


def _get_cursor(dsn: str) -> int:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT MAX(change_id) AS max_id FROM changes")
        ) as cur:
            row = cur.fetchone()
    return row["max_id"] if row and row["max_id"] is not None else 0


def _get_changes_since(
    dsn: str,
    cursor: int,
    max_size: int = _MAX_CHANGES_PER_PAGE,
) -> tuple[list[tuple[NodeId, bool, NodeRecord | None]], int, bool]:
    """Return (changes, new_cursor, has_more).

    Each change is (node_id, is_removed).
    """
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                """
            SELECT c.change_id, c.node_id, c.is_removed,
                   n.id,
                   n.parent_id, n.name, n.is_directory, n.created_time, n.modified_time, n.changed_time,
                   n.mime_type, n.hash, n.hash_algorithm, n.size, n.is_image, n.is_video,
                   n.width, n.height, n.ms_duration
            FROM changes c
            LEFT JOIN nodes n ON c.node_id = n.id
            WHERE c.change_id > ?
            ORDER BY c.change_id ASC
            LIMIT ?
            """,
                (cursor, max_size + 1),
            )
        ) as cur:
            rows = cur.fetchall()

    has_more = len(rows) > max_size
    rows = rows[:max_size]

    # Deduplicate within this raw page by node_id, keeping the last row
    # (largest change_id) for each node.
    latest_by_node_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest_by_node_id[row["node_id"]] = row

    deduped_rows = sorted(
        latest_by_node_id.values(),
        key=lambda row: int(row["change_id"]),
    )

    result: list[tuple[NodeId, bool, NodeRecord | None]] = []
    for row in deduped_rows:
        is_removed = bool(row["is_removed"])
        record: NodeRecord | None = None
        if not is_removed and row["name"] is not None:
            record = row_to_record(row)
        result.append((NodeId(row["node_id"]), is_removed, record))

    new_cursor = rows[-1]["change_id"] if rows else cursor
    return result, new_cursor, has_more


def row_to_record(row: sqlite3.Row) -> NodeRecord:
    return NodeRecord(
        id=NodeId(row["id"] if "id" in row.keys() else row["node_id"]),
        parent_id=(NodeId(row["parent_id"]) if row["parent_id"] is not None else None),
        name=row["name"],
        is_directory=bool(row["is_directory"]),
        created_time=row["created_time"],
        modified_time=row["modified_time"],
        changed_time=row["changed_time"],
        mime_type=row["mime_type"],
        hash=row["hash"],
        hash_algorithm=row["hash_algorithm"],
        size=row["size"],
        is_image=bool(row["is_image"]),
        is_video=bool(row["is_video"]),
        width=row["width"],
        height=row["height"],
        ms_duration=row["ms_duration"],
    )


_GC_CREATE_SQL = """
    CREATE TEMP TABLE _dangling AS
    WITH RECURSIVE reachable(node_id, owner) AS (
        SELECT ?, NULL
        UNION ALL
        SELECT n.id,
               CASE WHEN r.node_id = ?
                    THEN (SELECT o.key FROM temp._owners o WHERE o.name = n.name)
                    ELSE r.owner END
        FROM nodes n
        JOIN reachable r ON n.parent_id = r.node_id
        WHERE r.owner IS NULL
           OR EXISTS (
               SELECT 1 FROM node_ids i WHERE i.id = n.id AND i.backend = r.owner
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
    them. Then ID map rows with no node and no pending enrichment are pruned.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
        con.execute("CREATE TEMP TABLE _owners (name TEXT PRIMARY KEY, key TEXT)")
        # An instance never registered owns no node, so its mounts lose theirs.
        con.executemany(
            """
            INSERT INTO temp._owners (name, key) VALUES
                (?, COALESCE((SELECT key FROM backends WHERE name = ?), ''))
            """,
            list((mounts or {}).items()),
        )
        con.execute(_GC_CREATE_SQL, (str(SERVER_ROOT_ID), str(SERVER_ROOT_ID)))
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
            con.execute(
                "DELETE FROM pending_enrichment"
                " WHERE node_id IN (SELECT node_id FROM temp._dangling)"
            )
        con.execute(
            """
            DELETE FROM node_ids
            WHERE id NOT IN (SELECT id FROM nodes)
              AND id NOT IN (SELECT node_id FROM pending_enrichment)
            """
        )
        con.execute("DROP TABLE temp._dangling")
        con.execute("DROP TABLE temp._owners")
    return count


def registered_backends(dsn: str) -> list[str]:
    """Names of the instances registered in database *dsn*; none if it is new.

    Reads without creating or checking the schema, so command-line parsing can
    offer instances that are no longer configured.
    """
    if not os.path.exists(dsn):
        return []
    try:
        with _read_only(dsn) as con:
            with closing(con.execute("SELECT name FROM backends ORDER BY name")) as cur:
                return [str(row["name"]) for row in cur]
    except sqlite3.Error:
        return []


def _instance_key(con: sqlite3.Connection, name: str) -> str:
    with closing(
        con.execute("SELECT key FROM backends WHERE name = ?", (name,))
    ) as cur:
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"backend {name!r} is not registered in this database")
    return str(row["key"])


def _instance_tables(con: sqlite3.Connection, key: str) -> list[str]:
    """Tables named through the namespace with *key* (see ``BackendNamespace``)."""
    with closing(
        con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ) as cur:
        names = [str(row["name"]) for row in cur]
    return [n for n in names if n.endswith(f"_{key}")]


def _forget_instance(con: sqlite3.Connection, name: str, key: str) -> int:
    """Tombstone and delete the instance's nodes and drop its tables.

    Returns the number of nodes removed. Clients do not cascade removals, so
    each node gets its own tombstone.
    """
    owned_sql = "SELECT id FROM node_ids WHERE backend = ?"
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
    con.execute(
        f"DELETE FROM pending_enrichment WHERE backend = ? OR node_id IN ({owned_sql})",
        (name, key),
    )
    for table in _instance_tables(con, key):
        con.execute(f'DROP TABLE "{table}"')
    return count


def drop_backend(dsn: str, name: str) -> int:
    """Remove instance *name*: its nodes (with tombstones), IDs, tables, and
    registration, in one transaction. Returns the number of nodes removed.

    Raises:
        ValueError: If *name* is not registered.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
        key = _instance_key(con, name)
        count = _forget_instance(con, name, key)
        con.execute("DELETE FROM node_ids WHERE backend = ?", (key,))
        con.execute("DELETE FROM backends WHERE name = ?", (name,))
    return count


def rebuild_backend(dsn: str, name: str) -> int:
    """Remove instance *name*'s nodes (with tombstones) and tables, keeping its
    registration and IDs, so initial discovery on the next start rebuilds it.

    Items whose native IDs are stable come back under their old public IDs; IDs
    left unused are pruned by a later ``gc``. The tables are recreated at the
    backend's current version. Returns the number of nodes removed.

    Raises:
        ValueError: If *name* is not registered.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
        key = _instance_key(con, name)
        count = _forget_instance(con, name, key)
        con.execute("UPDATE backends SET schema_version = 0 WHERE name = ?", (name,))
    return count


def _release_reassigned_mounts(
    dsn: str, mounts: Mapping[NodeId, str], instances: _Instances
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
                    WHERE n.parent_id = ? AND i.backend IS NOT
                        (SELECT key FROM backends WHERE name = ?)
                    """,
                    (str(mount), backend),
                )
            ) as cur:
                foreign = [NodeId(row["id"]) for row in cur]
            for node_id in foreign:
                _delete_subtree_on_connection(con, node_id, instances)
            removed += len(foreign)
    return removed


def reset_change_history(dsn: str) -> int:
    """Clear changes and insert one update row per node except the server root.

    Also replaces the epoch, so consumers holding a cursor from the old history
    are told to start over. Returns the number of change records inserted.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
        con.execute(
            "UPDATE metadata SET value = ? WHERE key = 'epoch'", (_new_epoch(),)
        )
        con.execute("DELETE FROM changes")
        con.execute(
            "INSERT INTO changes (node_id, is_removed)"
            " SELECT id, 0 FROM nodes WHERE id != ?",
            (str(SERVER_ROOT_ID),),
        )
        with closing(con.execute("SELECT COUNT(*) AS n FROM changes")) as cur:
            return int(cur.fetchone()["n"])


def _build_deferred_preserved_set(
    dsn: str,
    seen_ids: set[NodeId],
    preserve_roots: set[NodeId],
) -> set[NodeId]:
    preserved = set(seen_ids)
    for root in preserve_roots:
        preserved |= _collect_subtree_node_ids(dsn, root)
    return preserved


def _resolve_path_to_id(dsn: str, segments: list[str]) -> NodeId | None:
    """Resolve a list of directory names to a final node_id using one recursive query.

    Matches only nodes where is_directory = 1.
    """
    if not segments:
        return SERVER_ROOT_ID

    # We build a 'path_segments' CTE that gives each name an index (lvl).
    # Then we recursively join 'nodes' where parent_id matches the previous
    # level's node_id and name matches the current level's segment name.
    # lvl_sql = " UNION ALL ".join(f"SELECT ? AS name, {i} AS lvl" for i in range(len(segments)))
    # Actually it's cleaner to use values if sqlite version permits, but
    # for compatibility we use the UNION ALL pattern.
    seg_queries = ["SELECT ? AS name, 0 AS lvl"]
    for i in range(1, len(segments)):
        seg_queries.append(f"SELECT ? AS name, {i} AS lvl")
    lvl_sql = " UNION ALL ".join(seg_queries)

    sql = f"""
    WITH RECURSIVE
    path_segments(name, lvl) AS (
        {lvl_sql}
    ),
    traversal(node_id, lvl) AS (
        SELECT n.id, 0
        FROM nodes n
        JOIN path_segments ps ON n.name = ps.name AND ps.lvl = 0
        WHERE n.parent_id = ? AND n.is_directory = 1
        UNION ALL
        SELECT n.id, t.lvl + 1
        FROM nodes n
        JOIN traversal t ON n.parent_id = t.node_id
        JOIN path_segments ps ON n.name = ps.name AND ps.lvl = t.lvl + 1
        WHERE n.is_directory = 1
    )
    SELECT node_id FROM traversal
    WHERE lvl = ?
    """
    params = list(segments) + [str(SERVER_ROOT_ID), len(segments) - 1]

    with _read_only(dsn) as con:
        with closing(con.execute(sql, params)) as cur:
            row = cur.fetchone()
    return NodeId(row["node_id"]) if row else None


class MirrorStore:
    """Async SQLite access - delegates blocking calls to OffMainService."""

    def __init__(
        self,
        dsn: str,
        *,
        off_main: OffMainService,
        schemas: Mapping[str, BackendSchema] | None = None,
    ) -> None:
        """*schemas* are registered by ``ensure_schema``, for instances that
        are not entered through a factory."""
        self._dsn = dsn
        self._off_main = off_main
        self._initial_schemas = dict(schemas or {})
        self._instances: dict[str, _Instance] = {}
        self._epoch = ""

    @property
    def epoch(self) -> str:
        """The change history's epoch, read by ``ensure_schema``.

        Only offline maintenance (``squash``) replaces it, so it stays fixed
        while the server runs.
        """
        return self._epoch

    async def ensure_schema(self) -> None:
        self._epoch = await self._off_main(_ensure_schema, self._dsn)
        for backend, schema in self._initial_schemas.items():
            await self.register_schema(backend, schema)

    async def register_backend(self, backend: str, type_: str = "") -> str:
        """Register instance *backend* of backend *type_*; returns its key.

        Raises:
            SchemaVersionError: If *backend* was registered with another type.
        """
        instance = self._instances.get(backend)
        if instance is not None:
            return instance.ns.key
        key = await self._off_main(_register_backend, self._dsn, backend, type_)
        self._instances[backend] = _Instance(ns=BackendNamespace(key))
        return key

    async def register_schema(
        self, backend: str, schema: BackendSchema
    ) -> BackendNamespace:
        """Persist instance *backend*'s state through *schema* from now on.

        Returns the namespace that names the instance's tables.

        Raises:
            SchemaVersionError: If the instance's tables have another version.
        """
        await self.register_backend(backend)
        ns = self._instances[backend].ns
        await self._off_main(_register_schema, self._dsn, backend, ns, schema)
        self._instances[backend] = _Instance(ns=ns, schema=schema)
        return ns

    def namespace(self, backend: str) -> BackendNamespace:
        """The namespace of registered instance *backend*."""
        return self._instances[backend].ns

    async def registered_backends(self) -> list[str]:
        """Names of every instance this database has seen, configured or not."""
        return await self._off_main(registered_backends, self._dsn)

    async def release_reassigned_mounts(self, mounts: Mapping[NodeId, str]) -> int:
        """Remove what other instances left under mounts now served by another.

        *mounts* maps mount node IDs to their configured instance. Run before
        backends start, so the new owner discovers the mount from scratch.
        Returns the number of subtrees removed.
        """
        return await self._off_main(
            _release_reassigned_mounts, self._dsn, mounts, self._instances
        )

    async def owner_of(self, node_id: NodeId) -> str | None:
        """The instance whose node *node_id* is; ``None`` for core nodes."""
        return await self._off_main(_owner_of, self._dsn, node_id)

    async def read[**A, R](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], R],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> R:
        """Run *fn* on a read-only connection; for backend-owned queries."""
        return await self._off_main(_run_read, self._dsn, fn, args, kwargs)

    async def write[**A, R](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], R],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> R:
        """Run *fn* in one write transaction; call it from the write queue."""
        return await self._off_main(_run_write, self._dsn, fn, args, kwargs)

    async def checkpoint(self) -> None:
        await self._off_main.untimed(_checkpoint, self._dsn)

    async def bulk_upsert_nodes(self, records: list[NodeRecord]) -> None:
        await self._off_main(_bulk_upsert_nodes, self._dsn, records)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        await self._off_main(_upsert_node_and_emit_change, self._dsn, record)

    async def delete_subtree_and_emit_changes(self, node_id: NodeId) -> None:
        await self._off_main(
            _delete_subtree_and_emit_changes, self._dsn, node_id, self._instances
        )

    async def apply_observations(
        self, observations: list[NodeObservation]
    ) -> set[NodeId]:
        return await self._off_main(
            _apply_observations, self._dsn, observations, self._instances
        )

    async def mark_enrichment_pending(
        self, node_id: NodeId, backend: str | None = None
    ) -> None:
        await self._off_main(
            _mark_enrichment_pending, self._dsn, node_id, backend, self._instances
        )

    async def get_pending_enrichment(self, backend: str | None = None) -> list[NodeId]:
        return await self._off_main(_get_pending_enrichment, self._dsn, backend)

    async def resolve_path_to_id(self, segments: list[str]) -> NodeId | None:
        return await self._off_main(_resolve_path_to_id, self._dsn, segments)

    async def get_node_by_id(self, node_id: NodeId) -> NodeRecord | None:
        return await self._off_main(_get_node_by_id, self._dsn, node_id)

    async def get_nodes_by_ids(
        self,
        node_ids: list[NodeId],
    ) -> dict[NodeId, NodeRecord]:
        return await self._off_main(_get_nodes_by_ids, self._dsn, node_ids)

    async def get_children(self, parent_id: NodeId) -> list[NodeRecord]:
        return await self._off_main(_get_children, self._dsn, parent_id)

    async def get_ancestors(self, node_id: NodeId) -> list[NodeRecord]:
        return await self._off_main(_get_ancestors, self._dsn, node_id)

    async def collect_subtree_node_ids(
        self,
        root_id: NodeId,
    ) -> set[NodeId]:
        return await self._off_main(_collect_subtree_node_ids, self._dsn, root_id)

    async def build_deferred_preserved_set(
        self,
        seen_ids: set[NodeId],
        preserve_roots: set[NodeId],
    ) -> set[NodeId]:
        return await self._off_main(
            _build_deferred_preserved_set, self._dsn, seen_ids, preserve_roots
        )

    async def deferred_removal_roots(
        self,
        preserved: set[NodeId],
        scope: set[NodeId],
    ) -> list[NodeId]:
        return await self._off_main(
            _deferred_removal_roots, self._dsn, preserved, scope
        )

    async def get_cursor(self) -> int:
        return await self._off_main(_get_cursor, self._dsn)

    async def get_changes_since(
        self,
        cursor: int,
        max_size: int = _MAX_CHANGES_PER_PAGE,
    ) -> tuple[list[tuple[NodeId, bool, NodeRecord | None]], int, bool]:
        return await self._off_main(_get_changes_since, self._dsn, cursor, max_size)

    async def cleanup_dangling_nodes(
        self, *, mounts: Mapping[str, str] | None = None
    ) -> int:
        return await self._off_main(
            partial(cleanup_dangling_nodes, mounts=mounts), self._dsn
        )

    async def reset_change_history(self) -> int:
        return await self._off_main(reset_change_history, self._dsn)


async def create_mirror_store(
    database_url: str,
    *,
    off_main: OffMainService,
    schemas: Mapping[str, BackendSchema] | None = None,
) -> MirrorStore:
    mirror = MirrorStore(database_url, off_main=off_main, schemas=schemas)
    _L.info("initializing database: %s", database_url)
    await mirror.ensure_schema()
    return mirror

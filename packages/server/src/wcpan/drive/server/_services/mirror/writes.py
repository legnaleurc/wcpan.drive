"""Node writes. Each emits its changes in the same transaction, except
``bulk_upsert_nodes``, which writes nodes only."""

import sqlite3
from contextlib import closing

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from ..._types import NodeObservation
from .model import Instance, Instances, registered, with_schema
from .reads import select_root_id


_UPSERT_NODE_SQL = """
            INSERT INTO nodes
                (id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, hash_algorithm, size, is_image, is_video, width, height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                height=excluded.height
            """


# Variant used by scan and webhook paths: preserves existing non-zero width/height
# so that media info set by the API upload handler is not overwritten with zeros.
_PRESERVE_MEDIA_UPSERT_SQL = """
            INSERT INTO nodes
                (id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, hash_algorithm, size, is_image, is_video, width, height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                height=CASE WHEN excluded.height > 0 THEN excluded.height ELSE nodes.height END
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
    )


def _observer_of(
    instances: Instances, observation: NodeObservation, *, root_id: NodeId | None
) -> Instance | None:
    """The registered instance that made *observation*; None for a core node.

    Only the core places nodes at the root or directly under it.

    Raises:
        ValueError: If the observation names an unregistered instance, carries
            a locator without naming its instance, or places a backend node at
            or directly under the root.
    """
    record = observation.record
    if observation.backend is None:
        if observation.locator is not None:
            raise ValueError(f"Untagged observation of {record.id} has a locator")
        return None
    instance = registered(instances, observation.backend)
    if record.parent_id is None or record.parent_id == root_id:
        raise ValueError(
            f"Backend {observation.backend!r} placed {record.id} at the root"
        )
    return instance


def _claim_id(con: sqlite3.Connection, node_id: NodeId, instance: Instance) -> None:
    """Give a node minted by its backend (``NodeIdMap.mint``) its map row."""
    con.execute(
        "INSERT OR IGNORE INTO node_ids (id, instance, native) VALUES (?, ?, ?)",
        (str(node_id), instance.ns.key, str(node_id)),
    )


def upsert_node_and_emit_change(dsn: str, record: NodeRecord) -> None:
    with _read_write(dsn) as con:
        con.execute(_PRESERVE_MEDIA_UPSERT_SQL, _node_row_values(record))
        con.execute(
            "INSERT INTO changes (node_id, is_removed) VALUES (?, 0)",
            (str(record.id),),
        )


def delete_subtree_on_connection(
    con: sqlite3.Connection,
    node_id: NodeId,
    instances: Instances,
) -> None:
    """Delete node and all descendants; emit remove-changes for each.

    Also drops the media failure records keyed by their native IDs (Synology
    permanent links), including *node_id*'s when it was never published.
    """
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
    for instance in with_schema(instances):
        assert instance.schema is not None
        instance.schema.forget_nodes(con, instance.ns, to_delete)
    con.execute(
        f"""
        DELETE FROM media_failures WHERE EXISTS (
            SELECT 1 FROM node_ids i
            WHERE i.id IN ({placeholders})
              AND i.instance = media_failures.instance
              AND i.native = media_failures.source_key
        )
        """,
        to_delete,
    )
    con.executemany(
        "INSERT INTO changes (node_id, is_removed) VALUES (?, 1)",
        [(nid,) for nid in to_delete],
    )


def delete_subtree_and_emit_changes(
    dsn: str, node_id: NodeId, instances: Instances
) -> None:
    with _read_write(dsn) as con:
        delete_subtree_on_connection(con, node_id, instances)


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


def apply_observations(
    dsn: str, observations: list[NodeObservation], instances: Instances
) -> set[NodeId]:
    """Upsert nodes and their locators in order, skipping any whose parent is gone.

    The parent check and writes share one explicit transaction, so an observation
    made before its parent was removed cannot recreate an orphan. A change is
    emitted only when the public node row actually changes; locator-only updates
    are silent. An applied observation clears the media failure record of its
    source key. Returns the IDs of the nodes that were applied.
    """
    if not observations:
        return set()
    applied: set[NodeId] = set()
    with _read_write(dsn) as con:
        root_id = select_root_id(con)
        for observation in observations:
            record = observation.record
            # Before the parent check, so an invalid observation is refused
            # whatever the mirror holds.
            owner = _observer_of(instances, observation, root_id=root_id)
            if record.parent_id is not None and not _node_exists(con, record.parent_id):
                continue
            before = _select_node_row(con, record.id)
            con.execute(_PRESERVE_MEDIA_UPSERT_SQL, _node_row_values(record))
            if _select_node_row(con, record.id) != before:
                con.execute(
                    "INSERT INTO changes (node_id, is_removed) VALUES (?, 0)",
                    (str(record.id),),
                )
            if owner is not None:
                _claim_id(con, record.id, owner)
                if observation.locator is not None and owner.schema is not None:
                    owner.schema.bind_locator(
                        con, owner.ns, record.id, observation.locator
                    )
                if observation.source_key is not None:
                    con.execute(
                        "DELETE FROM media_failures"
                        " WHERE instance = ? AND source_key = ?",
                        (owner.ns.key, observation.source_key),
                    )
            applied.add(record.id)
    return applied


def bulk_upsert_nodes(dsn: str, records: list[NodeRecord]) -> None:
    """Upsert nodes only (no change rows) in one transaction."""
    if not records:
        return
    with _read_write(dsn) as con:
        con.executemany(_UPSERT_NODE_SQL, [_node_row_values(r) for r in records])

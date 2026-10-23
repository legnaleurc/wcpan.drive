"""Node reads, path resolution, reconciliation queries, and the change feed."""

import sqlite3
from contextlib import closing

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.sqlite import (
    read_only as _read_only,
)


MAX_CHANGES_PER_PAGE = 1000


_ROOT_ID_SQL = "SELECT id FROM nodes WHERE parent_id IS NULL"


def get_node_by_id(dsn: str, node_id: NodeId) -> NodeRecord | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE id = ?", (str(node_id),))
        ) as cur:
            row = cur.fetchone()
    return row_to_record(row) if row else None


def get_nodes_by_ids(
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


def select_root_id(con: sqlite3.Connection) -> NodeId | None:
    """The server root's ID, or ``None`` before the root is created."""
    with closing(con.execute(_ROOT_ID_SQL)) as cur:
        row = cur.fetchone()
    return NodeId(row["id"]) if row is not None else None


def get_root(dsn: str) -> NodeRecord | None:
    with _read_only(dsn) as con:
        with closing(con.execute("SELECT * FROM nodes WHERE parent_id IS NULL")) as cur:
            row = cur.fetchone()
    return row_to_record(row) if row is not None else None


def get_children(dsn: str, parent_id: NodeId) -> list[NodeRecord]:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE parent_id = ?", (str(parent_id),))
        ) as cur:
            rows = cur.fetchall()
    return [row_to_record(r) for r in rows]


def collect_subtree_node_ids(dsn: str, root_id: NodeId) -> set[NodeId]:
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


def get_ancestors(dsn: str, node_id: NodeId) -> list[NodeRecord]:
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
        JOIN nodes p ON p.id = n.parent_id
        WHERE p.parent_id IS NOT NULL
    )
    SELECT * FROM ancestors
    """
    with _read_only(dsn) as con:
        with closing(con.execute(sql, (str(node_id),))) as cur:
            return [row_to_record(r) for r in cur.fetchall()]


def deferred_removal_roots(
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
    SELECT node_id, parent_id FROM descendants
    WHERE parent_id NOT IN (SELECT id FROM nodes WHERE parent_id IS NULL)
    """
    params = tuple(str(_) for _ in scope)
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


def build_deferred_preserved_set(
    dsn: str,
    seen_ids: set[NodeId],
    preserve_roots: set[NodeId],
) -> set[NodeId]:
    preserved = set(seen_ids)
    for root in preserve_roots:
        preserved |= collect_subtree_node_ids(dsn, root)
    return preserved


def resolve_path_to_id(dsn: str, segments: list[str]) -> NodeId | None:
    """Resolve a list of directory names to a final node_id using one recursive query.

    Matches only nodes where is_directory = 1. No segments name the root;
    ``None`` if there is no such directory.
    """
    if not segments:
        with _read_only(dsn) as con:
            return select_root_id(con)

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
        WHERE n.parent_id = (SELECT id FROM nodes WHERE parent_id IS NULL)
          AND n.is_directory = 1
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
    params = list(segments) + [len(segments) - 1]

    with _read_only(dsn) as con:
        with closing(con.execute(sql, params)) as cur:
            row = cur.fetchone()
    return NodeId(row["node_id"]) if row else None


def get_cursor(dsn: str) -> int:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT MAX(change_id) AS max_id FROM changes")
        ) as cur:
            row = cur.fetchone()
    return row["max_id"] if row and row["max_id"] is not None else 0


def get_changes_since(
    dsn: str,
    cursor: int,
    max_size: int = MAX_CHANGES_PER_PAGE,
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
                   n.width, n.height
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
    )

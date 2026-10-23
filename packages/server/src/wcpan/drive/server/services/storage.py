import logging
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager

from wcpan.drive.protocol.types import MirrorMutableId, MirrorStableId, NodeRecord
from wcpan.synology import SynologyPath

from ..lib.mounts import SERVER_ROOT_ID
from .off_main import OffMainService


_L = logging.getLogger(__name__)


class SchemaVersionError(RuntimeError):
    pass


_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA cache_size = -65536;

CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT    PRIMARY KEY,
    mutable_id   TEXT    NOT NULL,
    parent_id    TEXT,
    name         TEXT    NOT NULL,
    is_directory INTEGER NOT NULL DEFAULT 0,
    created_time INTEGER NOT NULL,
    modified_time INTEGER NOT NULL,
    changed_time INTEGER NOT NULL,
    mime_type    TEXT    NOT NULL DEFAULT '',
    hash         TEXT    NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    is_image     INTEGER NOT NULL DEFAULT 0,
    is_video     INTEGER NOT NULL DEFAULT 0,
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0,
    ms_duration  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_nodes_parent_id ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS ix_nodes_mutable_id ON nodes(mutable_id);

CREATE TABLE IF NOT EXISTS changes (
    change_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT    NOT NULL,
    is_removed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_changes_node_id ON changes(node_id);

CREATE TABLE IF NOT EXISTS mounts (
    name   TEXT PRIMARY KEY,
    max_id INTEGER NOT NULL,
    path   TEXT NOT NULL
);
"""

_SCHEMA_VERSION = 3
_MAX_CHANGES_PER_PAGE = 1000

_UPSERT_NODE_SQL = """
            INSERT INTO nodes
                (id, mutable_id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, size, is_image, is_video, width, height, ms_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mutable_id=excluded.mutable_id,
                parent_id=excluded.parent_id,
                name=excluded.name,
                is_directory=excluded.is_directory,
                created_time=excluded.created_time,
                modified_time=excluded.modified_time,
                changed_time=excluded.changed_time,
                mime_type=excluded.mime_type,
                hash=excluded.hash,
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
                (id, mutable_id, parent_id, name, is_directory, created_time, modified_time, changed_time,
                 mime_type, hash, size, is_image, is_video, width, height, ms_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mutable_id=excluded.mutable_id,
                parent_id=excluded.parent_id,
                name=excluded.name,
                is_directory=excluded.is_directory,
                created_time=excluded.created_time,
                modified_time=excluded.modified_time,
                changed_time=excluded.changed_time,
                mime_type=excluded.mime_type,
                hash=excluded.hash,
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
    str,
    str | None,
    str,
    int,
    int,
    int,
    int,
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
        str(record.mutable_id),
        str(record.parent_id) if record.parent_id is not None else None,
        record.name,
        1 if record.is_directory else 0,
        record.created_time,
        record.modified_time,
        record.changed_time,
        record.mime_type,
        record.hash,
        record.size,
        1 if record.is_image else 0,
        1 if record.is_video else 0,
        record.width,
        record.height,
        record.ms_duration,
    )


def _open(dsn: str) -> sqlite3.Connection:
    con = sqlite3.connect(dsn, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def _read_only(dsn: str) -> Generator[sqlite3.Connection]:
    """Open for SELECT-only; accidental writes raise at SQLite level."""
    con = _open(dsn)
    try:
        con.execute("PRAGMA query_only = ON")
        yield con
    finally:
        con.close()


@contextmanager
def _read_write(dsn: str) -> Generator[sqlite3.Connection]:
    con = _open(dsn)
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _ensure_schema(dsn: str) -> None:
    with _read_write(dsn) as con:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        existing_tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        has_user_tables = bool(existing_tables - {"sqlite_sequence"})
        if version == 0 and has_user_tables:
            raise SchemaVersionError(
                f"database schema version mismatch: expected {_SCHEMA_VERSION}, got 0"
            )
        if version not in (0, _SCHEMA_VERSION):
            raise SchemaVersionError(
                "database schema version mismatch: "
                f"expected {_SCHEMA_VERSION}, got {version}"
            )
        con.executescript(_DDL)
        if version == 0:
            con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _checkpoint(dsn: str) -> None:
    with _read_write(dsn) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _get_mount_max_ids(dsn: str, mounts: dict[str, SynologyPath]) -> dict[str, int]:
    """Return per-mount last_max_id values, resetting when path changes."""
    names = list(mounts.keys())
    if not names:
        return {}

    with _read_only(dsn) as con:
        placeholders = ",".join("?" * len(names))
        with closing(
            con.execute(
                f"SELECT name, max_id, path FROM mounts WHERE name IN ({placeholders})",
                names,
            )
        ) as cur:
            by_name = {
                row["name"]: (int(row["max_id"]), row["path"]) for row in cur.fetchall()
            }

    result: dict[str, int] = {}
    for name, syno_path in mounts.items():
        state = by_name.get(name)
        if state is None:
            result[name] = 0
            continue
        value, stored_path = state
        result[name] = value if stored_path == syno_path.path else 0

    return result


def _set_mount_state(dsn: str, name: str, path: str, value: int) -> None:
    """Save per-mount last_max_id and syno_path atomically."""
    with _read_write(dsn) as con:
        con.execute(
            """
            INSERT INTO mounts (name, max_id, path) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                max_id = excluded.max_id,
                path = excluded.path
            """,
            (name, value, path),
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
    node_id: MirrorStableId,
) -> None:
    """Delete node and all descendants; emit remove-changes for each."""
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
    con.executemany(
        "INSERT INTO changes (node_id, is_removed) VALUES (?, 1)",
        [(nid,) for nid in to_delete],
    )


def _delete_subtree_and_emit_changes(dsn: str, node_id: MirrorStableId) -> None:
    with _read_write(dsn) as con:
        _delete_subtree_on_connection(con, node_id)


def _bulk_upsert_nodes(dsn: str, records: list[NodeRecord]) -> None:
    """Upsert nodes only (no change rows) in one transaction."""
    if not records:
        return
    with _read_write(dsn) as con:
        con.executemany(_UPSERT_NODE_SQL, [_node_row_values(r) for r in records])


def _apply_scan_folder_batch(
    dsn: str,
    delete_roots: list[MirrorStableId],
    upserts: list[NodeRecord],
) -> None:
    """Apply all subtree removals and upserts for one polled folder in one transaction.

    Deletes run first (same order as *delete_roots), then upserts. API-driven
    writes still interleave on the write queue as separate jobs before/after this
    batch.
    """
    if not delete_roots and not upserts:
        return
    with _read_write(dsn) as con:
        for root in delete_roots:
            _delete_subtree_on_connection(con, root)
        con.executemany(
            _PRESERVE_MEDIA_UPSERT_SQL, [_node_row_values(r) for r in upserts]
        )
        con.executemany(
            "INSERT INTO changes (node_id, is_removed) VALUES (?, 0)",
            [(str(r.id),) for r in upserts],
        )


def _get_node_by_id(dsn: str, node_id: MirrorStableId) -> NodeRecord | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE id = ?", (str(node_id),))
        ) as cur:
            row = cur.fetchone()
    return _row_to_record(row) if row else None


def _get_nodes_by_ids(
    dsn: str,
    node_ids: list[MirrorStableId],
) -> dict[MirrorStableId, NodeRecord]:
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
    return {MirrorStableId(row["id"]): _row_to_record(row) for row in rows}


def _get_node_by_mutable_id(
    dsn: str,
    mutable_id: MirrorMutableId,
) -> NodeRecord | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                "SELECT * FROM nodes WHERE mutable_id = ?",
                (mutable_id,),
            )
        ) as cur:
            row = cur.fetchone()
    return _row_to_record(row) if row else None


def _get_children(dsn: str, parent_id: MirrorStableId) -> list[NodeRecord]:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT * FROM nodes WHERE parent_id = ?", (str(parent_id),))
        ) as cur:
            rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def _collect_subtree_node_ids(dsn: str, root_id: MirrorStableId) -> set[MirrorStableId]:
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
            return {MirrorStableId(row["node_id"]) for row in cur}


def _get_ancestors(dsn: str, node_id: MirrorStableId) -> list[NodeRecord]:
    """Fetch the ancestor chain starting from *node_id*, stopping before virtual nodes.

    Returns all nodes from *node_id* up to (but not including) the first virtual
    ancestor (node_id starting with '_'). Single query via recursive CTE.
    """
    sql = """
    WITH RECURSIVE ancestors AS (
        SELECT * FROM nodes WHERE id = ?
        UNION ALL
        SELECT n.* FROM nodes n
        JOIN ancestors a ON n.id = a.parent_id
        WHERE a.parent_id IS NOT NULL AND a.parent_id NOT GLOB '_*'
    )
    SELECT * FROM ancestors
    """
    with _read_only(dsn) as con:
        with closing(con.execute(sql, (str(node_id),))) as cur:
            return [_row_to_record(r) for r in cur.fetchall()]


def _apply_deferred_scan_removals(
    dsn: str,
    preserved: set[MirrorStableId],
    mount_ids: set[MirrorStableId],
) -> None:
    """Remove Synology subtrees under *mount_ids* that are not in *preserved*."""
    if not mount_ids:
        return

    placeholders = ",".join("?" * len(mount_ids))
    # Collect all non-virtual descendants of the mount nodes together with their
    # parent_ids so we can compute deletion roots in one transaction.
    sql = f"""
    WITH RECURSIVE descendants(node_id, parent_id) AS (
        SELECT id, parent_id FROM nodes WHERE id IN ({placeholders})
        UNION ALL
        SELECT n.id, n.parent_id FROM nodes n
        JOIN descendants d ON n.parent_id = d.node_id
    )
    SELECT node_id, parent_id FROM descendants WHERE node_id NOT GLOB '_*'
    """
    with _read_write(dsn) as con:
        with closing(con.execute(sql, tuple(str(_) for _ in mount_ids))) as cur:
            rows = cur.fetchall()

        parent_by_id: dict[MirrorStableId, MirrorStableId | None] = {
            MirrorStableId(row["node_id"]): (
                MirrorStableId(row["parent_id"])
                if row["parent_id"] is not None
                else None
            )
            for row in rows
        }
        candidates = {nid for nid in parent_by_id if nid not in preserved}
        if not candidates:
            return

        roots = sorted(
            nid for nid in candidates if parent_by_id.get(nid) not in candidates
        )
        for root in roots:
            _delete_subtree_on_connection(con, root)


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
) -> tuple[list[tuple[MirrorStableId, bool, NodeRecord | None]], int, bool]:
    """Return (changes, new_cursor, has_more).

    Each change is (node_id, is_removed).
    """
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                """
            SELECT c.change_id, c.node_id, c.is_removed,
                   n.id,
                   n.mutable_id, n.parent_id, n.name, n.is_directory, n.created_time, n.modified_time, n.changed_time,
                   n.mime_type, n.hash, n.size, n.is_image, n.is_video,
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

    result: list[tuple[MirrorStableId, bool, NodeRecord | None]] = []
    for row in deduped_rows:
        is_removed = bool(row["is_removed"])
        record: NodeRecord | None = None
        if not is_removed and row["name"] is not None:
            record = _row_to_record(row)
        result.append((MirrorStableId(row["node_id"]), is_removed, record))

    new_cursor = rows[-1]["change_id"] if rows else cursor
    return result, new_cursor, has_more


def _row_to_record(row: sqlite3.Row) -> NodeRecord:
    return NodeRecord(
        id=MirrorStableId(row["id"] if "id" in row.keys() else row["node_id"]),
        mutable_id=MirrorMutableId(row["mutable_id"]),
        parent_id=(
            MirrorStableId(row["parent_id"]) if row["parent_id"] is not None else None
        ),
        name=row["name"],
        is_directory=bool(row["is_directory"]),
        created_time=row["created_time"],
        modified_time=row["modified_time"],
        changed_time=row["changed_time"],
        mime_type=row["mime_type"],
        hash=row["hash"],
        size=row["size"],
        is_image=bool(row["is_image"]),
        is_video=bool(row["is_video"]),
        width=row["width"],
        height=row["height"],
        ms_duration=row["ms_duration"],
    )


_GC_CREATE_SQL = """
    CREATE TEMP TABLE _dangling AS
    WITH RECURSIVE reachable(node_id) AS (
        SELECT ?
        UNION ALL
        SELECT n.id FROM nodes n
        JOIN reachable r ON n.parent_id = r.node_id
    )
    SELECT id AS node_id FROM nodes
    WHERE id NOT IN (SELECT node_id FROM reachable)
"""


def cleanup_dangling_nodes(dsn: str) -> int:
    """Delete nodes not reachable from the server root; emit removal changes.

    Returns the number of nodes removed.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
        con.execute(_GC_CREATE_SQL, (str(SERVER_ROOT_ID),))
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
        con.execute("DROP TABLE IF EXISTS temp._dangling")
    return count


def reset_change_history(dsn: str) -> int:
    """Clear changes and insert one update row per node except the server root.

    Returns the number of change records inserted.
    """
    _ensure_schema(dsn)
    with _read_write(dsn) as con:
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
    seen_ids: set[MirrorStableId],
    preserve_roots: set[MirrorStableId],
) -> set[MirrorStableId]:
    preserved = set(seen_ids)
    for root in preserve_roots:
        preserved |= _collect_subtree_node_ids(dsn, root)
    return preserved


def _resolve_path_to_id(dsn: str, segments: list[str]) -> MirrorStableId | None:
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
    return MirrorStableId(row["node_id"]) if row else None


class StorageService:
    """Async SQLite access - delegates blocking calls to OffMainService."""

    def __init__(self, dsn: str, *, off_main: OffMainService) -> None:
        self._dsn = dsn
        self._off_main = off_main

    async def ensure_schema(self) -> None:
        await self._off_main(_ensure_schema, self._dsn)

    async def checkpoint(self) -> None:
        await self._off_main.untimed(_checkpoint, self._dsn)

    async def get_mount_max_ids(
        self, mounts: dict[str, SynologyPath]
    ) -> dict[str, int]:
        return await self._off_main(_get_mount_max_ids, self._dsn, mounts)

    async def set_mount_state(self, name: str, path: str, value: int) -> None:
        await self._off_main(_set_mount_state, self._dsn, name, path, value)

    async def bulk_upsert_nodes(self, records: list[NodeRecord]) -> None:
        await self._off_main(_bulk_upsert_nodes, self._dsn, records)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        await self._off_main(_upsert_node_and_emit_change, self._dsn, record)

    async def delete_subtree_and_emit_changes(self, node_id: MirrorStableId) -> None:
        await self._off_main(_delete_subtree_and_emit_changes, self._dsn, node_id)

    async def apply_scan_folder_batch(
        self,
        delete_roots: list[MirrorStableId],
        upserts: list[NodeRecord],
    ) -> None:
        await self._off_main(_apply_scan_folder_batch, self._dsn, delete_roots, upserts)

    async def resolve_path_to_id(self, segments: list[str]) -> MirrorStableId | None:
        return await self._off_main(_resolve_path_to_id, self._dsn, segments)

    async def get_node_by_id(self, node_id: MirrorStableId) -> NodeRecord | None:
        return await self._off_main(_get_node_by_id, self._dsn, node_id)

    async def get_nodes_by_ids(
        self,
        node_ids: list[MirrorStableId],
    ) -> dict[MirrorStableId, NodeRecord]:
        return await self._off_main(_get_nodes_by_ids, self._dsn, node_ids)

    async def get_node_by_mutable_id(
        self,
        mutable_id: MirrorMutableId,
    ) -> NodeRecord | None:
        return await self._off_main(_get_node_by_mutable_id, self._dsn, mutable_id)

    async def get_children(self, parent_id: MirrorStableId) -> list[NodeRecord]:
        return await self._off_main(_get_children, self._dsn, parent_id)

    async def get_ancestors(self, node_id: MirrorStableId) -> list[NodeRecord]:
        return await self._off_main(_get_ancestors, self._dsn, node_id)

    async def collect_subtree_node_ids(
        self,
        root_id: MirrorStableId,
    ) -> set[MirrorStableId]:
        return await self._off_main(_collect_subtree_node_ids, self._dsn, root_id)

    async def build_deferred_preserved_set(
        self,
        seen_ids: set[MirrorStableId],
        preserve_roots: set[MirrorStableId],
    ) -> set[MirrorStableId]:
        return await self._off_main(
            _build_deferred_preserved_set, self._dsn, seen_ids, preserve_roots
        )

    async def apply_deferred_scan_removals(
        self,
        preserved: set[MirrorStableId],
        mount_ids: set[MirrorStableId],
    ) -> None:
        await self._off_main(
            _apply_deferred_scan_removals, self._dsn, preserved, mount_ids
        )

    async def get_cursor(self) -> int:
        return await self._off_main(_get_cursor, self._dsn)

    async def get_changes_since(
        self,
        cursor: int,
        max_size: int = _MAX_CHANGES_PER_PAGE,
    ) -> tuple[list[tuple[MirrorStableId, bool, NodeRecord | None]], int, bool]:
        return await self._off_main(_get_changes_since, self._dsn, cursor, max_size)

    async def cleanup_dangling_nodes(self) -> int:
        return await self._off_main(cleanup_dangling_nodes, self._dsn)

    async def reset_change_history(self) -> int:
        return await self._off_main(reset_change_history, self._dsn)


async def create_storage_service(
    database_url: str,
    *,
    off_main: OffMainService,
) -> StorageService:
    storage = StorageService(database_url, off_main=off_main)
    _L.info("initializing database: %s", database_url)
    await storage.ensure_schema()
    return storage

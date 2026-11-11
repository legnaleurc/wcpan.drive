"""Synology state in the mirror database: node locators and mount checkpoints.

The tables are created and maintained through ``BackendSchema`` hooks on the
core's connection, so locators commit together with their nodes.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.services.mirror import MirrorStore, row_to_record
from wcpan.drive.server.types import BackendLocator
from wcpan.synology import SynologyPath

from ._convert import SynologyLocator
from ._schema import DDL


class SynologySchema:
    """``BackendSchema`` hooks for the Synology tables."""

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        con.executescript(DDL)

    def bind_locator(
        self, con: sqlite3.Connection, node_id: NodeId, locator: BackendLocator
    ) -> None:
        con.execute(
            """
            INSERT INTO synology_locators (node_id, file_id) VALUES (?, ?)
            ON CONFLICT(node_id) DO UPDATE SET file_id = excluded.file_id
            """,
            (str(node_id), locator),
        )

    def forget_nodes(self, con: sqlite3.Connection, node_ids: Sequence[str]) -> None:
        placeholders = ",".join("?" * len(node_ids))
        con.execute(
            f"DELETE FROM synology_locators WHERE node_id IN ({placeholders})",
            list(node_ids),
        )

    def forget_orphans(self, con: sqlite3.Connection) -> None:
        con.execute(
            "DELETE FROM synology_locators WHERE node_id NOT IN (SELECT id FROM nodes)"
        )


SYNOLOGY_SCHEMA = SynologySchema()


class SynologyStore:
    """Queries over the Synology tables through the core mirror connection."""

    def __init__(self, *, mirror: MirrorStore) -> None:
        self._mirror = mirror

    async def get_node_by_locator(self, locator: SynologyLocator) -> NodeRecord | None:
        return await self._mirror.read(_get_node_by_locator, locator)

    async def get_locators(
        self, node_ids: list[NodeId]
    ) -> dict[NodeId, SynologyLocator]:
        return await self._mirror.read(_get_locators, node_ids)

    async def get_mount_max_ids(
        self, mounts: dict[str, SynologyPath]
    ) -> dict[str, int]:
        return await self._mirror.read(_get_mount_max_ids, mounts)

    async def set_mount_state(self, name: str, path: str, value: int) -> None:
        await self._mirror.write(_set_mount_state, name, path, value)


def _get_mount_max_ids(
    con: sqlite3.Connection, mounts: dict[str, SynologyPath]
) -> dict[str, int]:
    """Return per-mount last_max_id values, resetting when path changes."""
    names = list(mounts.keys())
    if not names:
        return {}

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


def _set_mount_state(con: sqlite3.Connection, name: str, path: str, value: int) -> None:
    """Save per-mount last_max_id and syno_path atomically."""
    con.execute(
        """
        INSERT INTO mounts (name, max_id, path) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            max_id = excluded.max_id,
            path = excluded.path
        """,
        (name, value, path),
    )


def _get_node_by_locator(
    con: sqlite3.Connection, locator: SynologyLocator
) -> NodeRecord | None:
    with closing(
        con.execute(
            """
            SELECT n.* FROM synology_locators l
            JOIN nodes n ON n.id = l.node_id
            WHERE l.file_id = ?
            """,
            (locator,),
        )
    ) as cur:
        row = cur.fetchone()
    return row_to_record(row) if row else None


def _get_locators(
    con: sqlite3.Connection, node_ids: list[NodeId]
) -> dict[NodeId, SynologyLocator]:
    if not node_ids:
        return {}
    placeholders = ",".join("?" * len(node_ids))
    with closing(
        con.execute(
            "SELECT node_id, file_id FROM synology_locators"
            f" WHERE node_id IN ({placeholders})",
            [str(_) for _ in node_ids],
        )
    ) as cur:
        rows = cur.fetchall()
    return {NodeId(row["node_id"]): SynologyLocator(row["file_id"]) for row in rows}

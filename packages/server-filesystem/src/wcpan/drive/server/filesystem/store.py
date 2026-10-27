"""Filesystem backend state in the mirror database.

``fs_entries`` binds each public node to the identity of the object it names and
the stat signature its checksum was computed from. Hard links share an identity,
so the identity column is indexed but not unique. ``fs_mounts`` records which
filesystem each mount was scanned on, so an unmounted source is not mistaken for
an empty directory.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.types import BackendLocator

from .handles import FileIdentity


_DDL = """
CREATE TABLE IF NOT EXISTS fs_entries (
    node_id  TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fs_entries_identity ON fs_entries(identity);

CREATE TABLE IF NOT EXISTS fs_mounts (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fsid TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class EntryState:
    """What a node's metadata was last observed from."""

    identity: FileIdentity
    size: int
    mtime_ns: int
    ctime_ns: int

    def to_locator(self) -> BackendLocator:
        return BackendLocator(
            f"{self.identity}|{self.size}|{self.mtime_ns}|{self.ctime_ns}"
        )

    @classmethod
    def from_locator(cls, locator: BackendLocator) -> "EntryState":
        identity, size, mtime_ns, ctime_ns = locator.rsplit("|", 3)
        return cls(FileIdentity(identity), int(size), int(mtime_ns), int(ctime_ns))


class FilesystemSchema:
    """``BackendSchema`` hooks for the filesystem tables."""

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        con.executescript(_DDL)

    def bind_locator(
        self, con: sqlite3.Connection, node_id: NodeId, locator: BackendLocator
    ) -> None:
        state = EntryState.from_locator(locator)
        con.execute(
            """
            INSERT INTO fs_entries (node_id, identity, size, mtime_ns, ctime_ns)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                identity = excluded.identity,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                ctime_ns = excluded.ctime_ns
            """,
            (str(node_id), state.identity, state.size, state.mtime_ns, state.ctime_ns),
        )

    def forget_nodes(self, con: sqlite3.Connection, node_ids: Sequence[str]) -> None:
        placeholders = ",".join("?" * len(node_ids))
        con.execute(
            f"DELETE FROM fs_entries WHERE node_id IN ({placeholders})", list(node_ids)
        )

    def forget_orphans(self, con: sqlite3.Connection) -> None:
        con.execute(
            "DELETE FROM fs_entries WHERE node_id NOT IN (SELECT id FROM nodes)"
        )


FILESYSTEM_SCHEMA = FilesystemSchema()


class FilesystemStore:
    """Queries over the filesystem tables through the core mirror connection."""

    def __init__(self, *, mirror: MirrorStore) -> None:
        self._mirror = mirror

    async def get_entries(self, node_ids: list[NodeId]) -> dict[NodeId, EntryState]:
        return await self._mirror.read(_get_entries, node_ids)

    async def nodes_by_identity(self, identity: FileIdentity) -> list[NodeId]:
        return await self._mirror.read(_nodes_by_identity, identity)

    async def get_mount_fsid(self, name: str, path: str) -> str | None:
        """The filesystem *name* was last scanned on, unless its path changed."""
        return await self._mirror.read(_get_mount_fsid, name, path)

    async def set_mount_fsid(self, name: str, path: str, fsid: str) -> None:
        await self._mirror.write(_set_mount_fsid, name, path, fsid)


def _get_entries(
    con: sqlite3.Connection, node_ids: list[NodeId]
) -> dict[NodeId, EntryState]:
    if not node_ids:
        return {}
    placeholders = ",".join("?" * len(node_ids))
    with closing(
        con.execute(
            "SELECT node_id, identity, size, mtime_ns, ctime_ns FROM fs_entries"
            f" WHERE node_id IN ({placeholders})",
            [str(_) for _ in node_ids],
        )
    ) as cur:
        rows = cur.fetchall()
    return {
        NodeId(row["node_id"]): EntryState(
            FileIdentity(row["identity"]),
            row["size"],
            row["mtime_ns"],
            row["ctime_ns"],
        )
        for row in rows
    }


def _nodes_by_identity(con: sqlite3.Connection, identity: FileIdentity) -> list[NodeId]:
    with closing(
        con.execute(
            """
            SELECT e.node_id FROM fs_entries e
            JOIN nodes n ON n.id = e.node_id
            WHERE e.identity = ?
            ORDER BY e.node_id
            """,
            (identity,),
        )
    ) as cur:
        return [NodeId(row["node_id"]) for row in cur.fetchall()]


def _get_mount_fsid(con: sqlite3.Connection, name: str, path: str) -> str | None:
    with closing(
        con.execute("SELECT path, fsid FROM fs_mounts WHERE name = ?", (name,))
    ) as cur:
        row = cur.fetchone()
    if row is None or row["path"] != path:
        return None
    return str(row["fsid"])


def _set_mount_fsid(con: sqlite3.Connection, name: str, path: str, fsid: str) -> None:
    con.execute(
        """
        INSERT INTO fs_mounts (name, path, fsid) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET path = excluded.path, fsid = excluded.fsid
        """,
        (name, path, fsid),
    )

"""Public node IDs, minted by the core and mapped to backend-native IDs.

Every node a backend serves has a row in ``node_ids`` naming the backend
instance (by registry key) and the native ID the backend uses with its source.
Public IDs are opaque random tokens, unique across instances by construction.
Rows never change once written; offline ``gc`` prunes the unused ones.
"""

import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from contextlib import closing
from typing import Protocol

from wcpan.drive.protocol.types import NodeId

from .mirror import MirrorStore


# Bound on parameters per statement, below SQLite's default limit.
_CHUNK = 500
# Cached mappings per instance; the cache is dropped when it grows past this.
_CACHE_SIZE = 65536


class UnknownNodeError(LookupError):
    """A public ID has no native ID in this backend instance."""


class NodeIdMap(Protocol):
    """Public IDs of one backend instance.

    Backends translate where they convert source data: every ID they exchange
    with the core is public, and native IDs appear only between a backend and
    its source.
    """

    async def public(self, natives: Sequence[str]) -> list[NodeId]:
        """The public ID of each native ID, minting those seen for the first time.

        Concurrent calls for one new native ID get the same public ID. Must not
        be called from a write-queue job.
        """
        ...

    async def lookup(self, native: str) -> NodeId | None:
        """The public ID of *native*, without minting one."""
        ...

    async def native_of(self, node_id: NodeId) -> str:
        """The native ID of *node_id*.

        Raises:
            UnknownNodeError: If *node_id* does not belong to this instance.
        """
        ...

    def mint(self) -> NodeId:
        """A fresh public ID that is its own native ID.

        For backends that keep their own mapping to the source. The map row is
        written with the node's first observation.
        """
        ...


def new_node_id() -> NodeId:
    return NodeId(uuid.uuid4().hex)


def _chunks[T](items: Sequence[T]) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _lookup(
    con: sqlite3.Connection, key: str, natives: Sequence[str]
) -> dict[str, NodeId]:
    found: dict[str, NodeId] = {}
    for chunk in _chunks(natives):
        placeholders = ",".join("?" * len(chunk))
        with closing(
            con.execute(
                "SELECT id, native FROM node_ids"
                f" WHERE backend = ? AND native IN ({placeholders})",
                (key, *chunk),
            )
        ) as cur:
            found.update((row["native"], NodeId(row["id"])) for row in cur)
    return found


def _mint(
    con: sqlite3.Connection, key: str, natives: Sequence[str]
) -> dict[str, NodeId]:
    # Another writer may have minted some meanwhile; theirs win.
    con.executemany(
        "INSERT OR IGNORE INTO node_ids (id, backend, native) VALUES (?, ?, ?)",
        [(new_node_id(), key, native) for native in natives],
    )
    return _lookup(con, key, natives)


def _native_of(con: sqlite3.Connection, key: str, node_id: NodeId) -> str | None:
    with closing(
        con.execute(
            "SELECT native FROM node_ids WHERE id = ? AND backend = ?",
            (str(node_id), key),
        )
    ) as cur:
        row = cur.fetchone()
    return str(row["native"]) if row is not None else None


class NodeIds:
    """``NodeIdMap`` over the mirror database for the instance with *key*."""

    def __init__(self, *, mirror: MirrorStore, key: str) -> None:
        self._mirror = mirror
        self._key = key
        self._by_native: dict[str, NodeId] = {}
        self._by_id: dict[NodeId, str] = {}

    async def public(self, natives: Sequence[str]) -> list[NodeId]:
        found = {n: self._by_native[n] for n in natives if n in self._by_native}
        missing = [n for n in dict.fromkeys(natives) if n not in found]
        if missing:
            found.update(await self._mirror.read(_lookup, self._key, missing))
            missing = [n for n in missing if n not in found]
        if missing:
            # Directly, not through the write queue: the insert is idempotent
            # and independent of the node's own write.
            found.update(await self._mirror.write(_mint, self._key, missing))
        for native, node_id in found.items():
            self._remember(native, node_id)
        return [found[n] for n in natives]

    async def lookup(self, native: str) -> NodeId | None:
        node_id = self._by_native.get(native)
        if node_id is None:
            node_id = (await self._mirror.read(_lookup, self._key, [native])).get(
                native
            )
            if node_id is not None:
                self._remember(native, node_id)
        return node_id

    async def native_of(self, node_id: NodeId) -> str:
        native = self._by_id.get(node_id)
        if native is None:
            native = await self._mirror.read(_native_of, self._key, node_id)
            if native is None:
                raise UnknownNodeError(node_id)
            self._remember(native, node_id)
        return native

    def mint(self) -> NodeId:
        return new_node_id()

    def _remember(self, native: str, node_id: NodeId) -> None:
        if len(self._by_id) >= _CACHE_SIZE:
            self._by_id.clear()
            self._by_native.clear()
        self._by_native[native] = node_id
        self._by_id[node_id] = native

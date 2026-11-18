"""One reconciliation pass over a scope, shared by startup scan and backfill.

Listings report what they saw and which folders they could not cover; removals
are decided once, at the end of the pass, rather than from any single listing.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from logging import getLogger

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..._types import NodeObservation, ReadSeq
from ..mirror.store import (
    MirrorReader,
)
from ..operations import Locate, LocateUndecidedError
from .ordering import NodeOrdering
from .service import NodeSyncService


_L = getLogger(__name__)

# Concurrent backend lookups when confirming removals.
_CONFIRM_CONCURRENCY = 8


class ReconcileSession:
    """Coverage and removals for one pass over the subtrees under *scope*.

    A mirrored node is a removal candidate only if no listing in the pass saw it,
    no uncovered folder contains it, and no read that started after the pass
    began touched it. A listing is not an atomic snapshot of the tree: a node can
    move into a folder already listed and out of one not yet listed. Each
    candidate subtree root is therefore fetched directly before it is removed.
    """

    def __init__(
        self,
        scope: set[NodeId],
        read: ReadSeq,
        *,
        mirror: MirrorReader,
        node_sync: NodeSyncService | NodeOrdering,
        locate: Locate,
    ) -> None:
        """Only ``commit_removals`` writes, and needs a ``NodeSyncService``;
        a dry run can pass the read ordering alone."""
        self._scope = scope
        self._read = read
        self._mirror = mirror
        self._node_sync = node_sync
        self._locate = locate
        self._seen = set[NodeId]()
        self._preserve_roots = set[NodeId]()

    def saw(self, node_ids: Iterable[NodeId]) -> None:
        """Record nodes reported by a successful listing."""
        self._seen.update(node_ids)

    def preserve(self, root: NodeId) -> None:
        """Keep the mirrored subtree under *root*; this pass did not cover it."""
        self._preserve_roots.add(root)

    async def removal_roots(self) -> list[NodeId]:
        """Roots of the mirrored subtrees no listing in this pass accounted for.

        Waits for queued writes, so nodes observed by newer reads are excluded.
        """
        preserved = await self._mirror.build_deferred_preserved_set(
            self._seen, self._preserve_roots
        )
        preserved |= await self._node_sync.touched_after(self._read)
        return await self._mirror.deferred_removal_roots(preserved, self._scope)

    async def commit_removals(self) -> list[NodeId]:
        """Confirm and apply removals; returns the subtree roots removed.

        Raises:
            RuntimeError: If the session was opened without a writer.
        """
        writer = self._node_sync
        if not isinstance(writer, NodeSyncService):
            raise RuntimeError("a read-only reconcile pass cannot remove nodes")
        roots = await self.removal_roots()
        semaphore = asyncio.Semaphore(_CONFIRM_CONCURRENCY)
        removed: list[NodeId] = []

        async def confirm(root: NodeId) -> None:
            async with semaphore:
                if await self._remove_unless_moved(root, writer):
                    removed.append(root)

        async with asyncio.TaskGroup() as group:
            for root in roots:
                group.create_task(confirm(root))
        return sorted(removed)

    async def _remove_unless_moved(
        self, node_id: NodeId, writer: NodeSyncService
    ) -> bool:
        """Remove *node_id* unless it still exists under a mirrored parent.

        Only an undecided lookup preserves the node; a node found elsewhere is
        recorded under its new parent instead.
        """
        async with writer.observe() as read:
            try:
                current = await self._locate(node_id)
            except LocateUndecidedError:
                _L.warning("Could not confirm removal of %s; preserving it", node_id)
                return False
            if current is not None:
                _L.debug(
                    "Node %s moved during reconcile to %s",
                    node_id,
                    current.record.parent_id,
                )
                await writer.upsert(current, read=read)
                return False
            await writer.delete(node_id, read=read)
            return True


@asynccontextmanager
async def reconcile_session(
    scope: set[NodeId],
    *,
    mirror: MirrorReader,
    node_sync: NodeSyncService | NodeOrdering,
    locate: Locate,
) -> AsyncGenerator[ReconcileSession, None]:
    """Open a pass over *scope*; enter before the first listing starts."""
    async with node_sync.observe() as read:
        yield ReconcileSession(
            scope, read, mirror=mirror, node_sync=node_sync, locate=locate
        )


class ReconcileWrites:
    """Writes from one listing of a pass, numbered by that listing's read."""

    def __init__(self, read: ReadSeq, *, writer: NodeSyncService) -> None:
        self._read = read
        self._writer = writer

    async def inserted(self, observation: NodeObservation) -> None:
        """Record a node the mirror lacks; a file is probed first."""
        await self._writer.reconcile_insert(observation, read=self._read)

    async def updated(self, observation: NodeObservation, existing: NodeRecord) -> None:
        """Replace *existing*, probing again unless its dimensions still apply."""
        await self._writer.reconcile_update(observation, existing, read=self._read)


class ReconcilePass:
    """A backend-driven pass comparing the source with the mirror under a scope.

    A dry run has no writer: its listings get no ``ReconcileWrites``, and it can
    report removal candidates but not commit them.
    """

    def __init__(
        self,
        session: ReconcileSession,
        *,
        ordering: NodeSyncService | NodeOrdering,
        writer: NodeSyncService | None,
    ) -> None:
        self._session = session
        self._ordering = ordering
        self._writer = writer

    @asynccontextmanager
    async def listing(self) -> AsyncGenerator[ReconcileWrites | None, None]:
        """Number one listing; enter before reading the source."""
        async with self._ordering.observe() as read:
            writer = self._writer
            yield None if writer is None else ReconcileWrites(read, writer=writer)

    def saw(self, node_ids: Iterable[NodeId]) -> None:
        """Record nodes reported by a successful listing."""
        self._session.saw(node_ids)

    def preserve(self, root: NodeId) -> None:
        """Keep the mirrored subtree under *root*; this pass did not cover it."""
        self._session.preserve(root)

    async def removal_roots(self) -> list[NodeId]:
        """Roots of the mirrored subtrees no listing accounted for."""
        return await self._session.removal_roots()

    async def commit_removals(self) -> list[NodeId]:
        """Confirm and apply removals; returns the subtree roots removed.

        Raises:
            RuntimeError: For a dry run.
        """
        if self._writer is None:
            raise RuntimeError("a dry run cannot remove nodes")
        return await self._session.commit_removals()


@asynccontextmanager
async def reconcile_pass(
    scope: set[NodeId],
    *,
    mirror: MirrorReader,
    node_sync: NodeSyncService | NodeOrdering,
    locate: Locate,
    dry_run: bool = False,
) -> AsyncGenerator[ReconcilePass, None]:
    """Open a pass over *scope*; enter before the first listing starts.

    Raises:
        RuntimeError: If it is not a dry run and *node_sync* cannot write.
    """
    writer = node_sync if isinstance(node_sync, NodeSyncService) else None
    if writer is None and not dry_run:
        raise RuntimeError("only a dry run can reconcile without a writer")
    async with reconcile_session(
        scope, mirror=mirror, node_sync=node_sync, locate=locate
    ) as session:
        yield ReconcilePass(
            session, ordering=node_sync, writer=None if dry_run else writer
        )

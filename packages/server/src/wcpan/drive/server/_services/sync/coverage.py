"""What one pass covered, and the removals it decides at the end.

Listings report what they saw and which folders they could not cover; removals
are decided once, at the end of the pass, rather than from any single listing.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from logging import getLogger

from wcpan.drive.protocol.types import NodeId

from ..._types import ReadSeq
from ..mirror.store import (
    MirrorReader,
)
from ..operations import Locate, LocateUndecidedError
from .writer import MirrorWriter


_L = getLogger(__name__)

# Concurrent backend lookups when confirming removals.
_CONFIRM_CONCURRENCY = 8


class Coverage:
    """What one pass's listings covered under *scope*; it needs no writer.

    A mirrored node is a removal candidate only if no listing in the pass saw
    it and no uncovered folder contains it. A dry run stops here and reports
    the candidates.
    """

    def __init__(self, scope: set[NodeId], *, mirror: MirrorReader) -> None:
        self._scope = scope
        self._mirror = mirror
        self._seen = set[NodeId]()
        self._preserve_roots = set[NodeId]()

    def saw(self, node_ids: Iterable[NodeId]) -> None:
        """Record nodes reported by a successful listing."""
        self._seen.update(node_ids)

    def preserve(self, root: NodeId) -> None:
        """Keep the mirrored subtree under *root*; this pass did not cover it."""
        self._preserve_roots.add(root)

    async def removal_roots(self) -> list[NodeId]:
        """Roots of the mirrored subtrees no listing in this pass accounted for."""
        return await self._uncovered(set())

    async def _uncovered(self, touched: set[NodeId]) -> list[NodeId]:
        preserved = await self._mirror.build_deferred_preserved_set(
            self._seen, self._preserve_roots
        )
        return await self._mirror.deferred_removal_roots(
            preserved | touched, self._scope
        )


class ReconcileSession(Coverage):
    """Coverage of a pass that writes, which also confirms and applies removals.

    A node a read that started after the pass began touched is no candidate
    either. A listing is not an atomic snapshot of the tree: a node can move
    into a folder already listed and out of one not yet listed. Each candidate
    subtree root is therefore fetched directly before it is removed.
    """

    def __init__(
        self,
        scope: set[NodeId],
        read: ReadSeq,
        *,
        mirror: MirrorReader,
        writer: MirrorWriter,
        locate: Locate,
    ) -> None:
        super().__init__(scope, mirror=mirror)
        self._read = read
        self._writer = writer
        self._locate = locate

    async def removal_roots(self) -> list[NodeId]:
        """Roots of the mirrored subtrees no listing in this pass accounted for.

        Waits for queued writes, so nodes observed by newer reads are excluded.
        """
        return await self._uncovered(await self._writer.touched_after(self._read))

    async def commit_removals(self) -> list[NodeId]:
        """Confirm and apply removals; returns the subtree roots removed."""
        roots = await self.removal_roots()
        semaphore = asyncio.Semaphore(_CONFIRM_CONCURRENCY)
        removed: list[NodeId] = []

        async def confirm(root: NodeId) -> None:
            async with semaphore:
                if await self._remove_unless_moved(root):
                    removed.append(root)

        async with asyncio.TaskGroup() as group:
            for root in roots:
                group.create_task(confirm(root))
        return sorted(removed)

    async def _remove_unless_moved(self, node_id: NodeId) -> bool:
        """Remove *node_id* unless it still exists under a mirrored parent.

        Only an undecided lookup preserves the node; a node found elsewhere is
        recorded under its new parent instead.
        """
        writer = self._writer
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
    writer: MirrorWriter,
    locate: Locate,
) -> AsyncGenerator[ReconcileSession, None]:
    """Open a pass over *scope*; enter before the first listing starts."""
    async with writer.observe() as read:
        yield ReconcileSession(scope, read, mirror=mirror, writer=writer, locate=locate)

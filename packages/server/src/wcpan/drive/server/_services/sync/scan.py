"""The scan runner and the scope through which a backend produces facts.

See "Backend framework" in the package ``SPEC.md``.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from typing import Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..._lib.bfs import parallel_bfs
from ..._types import NodeObservation, ReadSeq
from ..mirror.store import MirrorReader
from .reconcile import ReconcileSession, reconcile_session
from .service import NodeSyncService


_L = getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ScanPlan[F]:
    """What one pass covers, decided before any listing."""

    # Mirrored subtrees this pass reconciles.
    scope: set[NodeId]
    roots: list[F]
    # In scope but unusable: kept as mirrored, and the pass is incomplete.
    preserved: list[NodeId]


@dataclass(frozen=True, kw_only=True)
class Listing[F]:
    """One folder as the source reported it."""

    # Every child this listing accounted for.
    seen: list[NodeId]
    # The children to write.
    changed: list[NodeObservation]
    # Folders to visit next.
    subfolders: list[F]
    # Children deliberately not visited; their subtrees stay as mirrored.
    keep: list[NodeId]


class Scanner[F](ABC):
    """The source-specific part of one scan pass or walk.

    *F* is the backend's folder handle. It carries whatever listing the folder
    needs, since a folder can be listed before its own write lands.
    """

    # False: finish traversing one root before starting the next.
    concurrent_roots: bool = True

    @abstractmethod
    async def prepare(self) -> ScanPlan[F]:
        """Decide what this pass covers; a failure stops the pass."""

    @abstractmethod
    def folder_id(self, folder: F) -> NodeId:
        """The node of *folder*."""

    @abstractmethod
    async def list_folder(self, folder: F) -> Listing[F]:
        """List *folder*; a failure keeps its subtree as mirrored."""

    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        """Fetch one removal candidate directly (see ``Locate``)."""

    async def skip(self, folder: F) -> bool:
        """Whether *folder* is known unchanged, keeping its subtree."""
        return False

    async def root_finished(self, root: F, complete: bool) -> None:
        """Called once the pass's writes have landed, such as to save progress."""

    async def pass_finished(self, complete: bool) -> None:
        """Called after every ``root_finished``.

        *complete* is true if every root was fully covered and nothing in scope
        was preserved as unusable.
        """


class FolderLister[F](Protocol):
    """What ``ScanRunner.walk`` needs; every ``Scanner`` is one."""

    async def list_folder(self, folder: F) -> Listing[F]: ...


class Changes:
    """Writes facts from the read that an ``observe`` block numbered."""

    def __init__(self, read: ReadSeq, *, node_sync: NodeSyncService) -> None:
        self._read = read
        self._node_sync = node_sync

    async def upserted(
        self, observation: NodeObservation, *, strict: bool = False
    ) -> NodeRecord:
        """Enrich *observation* here, queue its write, and return its record.

        Raises:
            MediaEnrichmentError: With *strict*, if the media probe fails.
        """
        return await self._node_sync.upsert(observation, read=self._read, strict=strict)

    async def removed(self, node_id: NodeId) -> None:
        """Queue the removal of the subtree under *node_id*."""
        await self._node_sync.delete(node_id, read=self._read)

    async def landed(self) -> None:
        """Wait for every write queued so far, not for later ones.

        Raises:
            WriterStoppedError: The write worker ended first.
        """
        await self._node_sync.barrier()


@dataclass(kw_only=True)
class _RootProgress:
    complete: bool = True


class ScanRunner:
    """Runs one backend instance's scan passes, one at a time."""

    def __init__(self, *, mirror: MirrorReader, node_sync: NodeSyncService) -> None:
        self._mirror = mirror
        self._node_sync = node_sync
        self._pass_lock = asyncio.Lock()

    @asynccontextmanager
    async def observe(self, *, exclusive: bool = False) -> AsyncGenerator[Changes]:
        """Number a source read; enter before the first read facts depend on.

        With *exclusive*, no scan pass runs during the block or until its writes
        have landed, including when the block raises. Starting a pass inside
        such a block deadlocks.

        Raises:
            WriterStoppedError: With *exclusive*, if the write worker ended
                before the writes landed and the block itself succeeded.
        """
        if not exclusive:
            async with self._node_sync.observe() as read:
                yield Changes(read, node_sync=self._node_sync)
            return
        async with self._pass_lock, self._node_sync.observe() as read:
            changes = Changes(read, node_sync=self._node_sync)
            try:
                yield changes
            except Exception as failed:
                await _settle_after(failed, changes.landed)
                raise
            await changes.landed()

    async def run[F](self, scanner: Scanner[F]) -> None:
        """Run one reconciling pass of *scanner*, after any pass before it.

        Returns once removals, enrichment, and the finish hooks are done. Work
        a failed pass already submitted settles before the next pass can start,
        so that pass cannot give a file still being enriched a second ID.

        Raises:
            Exception: What failed in ``prepare``, traversal, or a finish hook.
        """
        async with self._pass_lock:
            plan = await scanner.prepare()
            try:
                await self._run_pass(scanner, plan)
            except Exception as failed:
                await _settle_after(failed, self._node_sync.wait_enrichment_drained)
                raise

    async def walk[F](self, lister: FolderLister[F], root: F) -> None:
        """Write everything *lister* lists under *root*, removing nothing.

        Takes no pass lock and calls neither ``skip`` nor the finish hooks.
        Directories land before their children are listed. File enrichment is
        submitted, not awaited.

        Raises:
            Exception: What failed in a listing.
        """

        async def visit(folder: F) -> list[F]:
            async with self._node_sync.observe() as read:
                listing = await lister.list_folder(folder)
                await self._submit(listing, read, land_directories=True)
            return listing.subfolders

        await _traverse([root], visit)

    async def _run_pass[F](self, scanner: Scanner[F], plan: ScanPlan[F]) -> None:
        work = [(root, _RootProgress()) for root in plan.roots]
        async with reconcile_session(
            plan.scope,
            mirror=self._mirror,
            node_sync=self._node_sync,
            locate=scanner.locate,
        ) as session:
            for node_id in plan.preserved:
                session.preserve(node_id)
            visit = partial(self._visit, scanner, session)
            if scanner.concurrent_roots:
                await _traverse(work, visit)
            else:
                for item in work:
                    await _traverse([item], visit)
            await session.commit_removals()
        await self._node_sync.wait_enrichment_drained()
        for root, progress in work:
            await scanner.root_finished(root, progress.complete)
        await scanner.pass_finished(
            not plan.preserved and all(p.complete for _, p in work)
        )

    async def _visit[F](
        self,
        scanner: Scanner[F],
        session: ReconcileSession,
        item: tuple[F, _RootProgress],
    ) -> list[tuple[F, _RootProgress]]:
        folder, progress = item
        folder_id = scanner.folder_id(folder)
        async with self._node_sync.observe() as read:
            try:
                if await scanner.skip(folder):
                    session.preserve(folder_id)
                    return []
                listing = await scanner.list_folder(folder)
            except Exception:
                _L.exception("Cannot list folder %s; keeping its mirror", folder_id)
                session.preserve(folder_id)
                progress.complete = False
                return []
            session.saw(listing.seen)
            for node_id in listing.keep:
                session.preserve(node_id)
            await self._submit(listing, read, land_directories=False)
        return [(child, progress) for child in listing.subfolders]

    async def _submit[F](
        self, listing: Listing[F], read: ReadSeq, *, land_directories: bool
    ) -> None:
        directories = [o for o in listing.changed if o.record.is_directory]
        files = [o for o in listing.changed if not o.record.is_directory]
        await self._node_sync.upsert_batch(directories, read=read)
        if land_directories and directories:
            await self._node_sync.barrier()
        await self._node_sync.upsert_file_batch(files, read=read)


async def _settle_after(
    failed: Exception, settle: Callable[[], Awaitable[None]]
) -> None:
    """Run *settle* for a failure; if it fails too, *failed* still propagates."""
    try:
        await settle()
    except Exception as e:
        raise failed from e


async def _traverse[T](
    initial: Iterable[T], visit: Callable[[T], Awaitable[list[T]]]
) -> None:
    """``parallel_bfs``, raising the first failure itself rather than a group."""
    try:
        await parallel_bfs(initial, visit)
    except ExceptionGroup as group:
        raise group.exceptions[0] from group

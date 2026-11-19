"""The scan runner and the scope through which a backend produces facts.

See "Backend framework" in the package ``SPEC.md``.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from typing import Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..._lib.bfs import parallel_bfs
from ..._lib.records import same_public_metadata
from ..._types import NodeObservation, ReadSeq
from ..mirror.store import MirrorReader
from .reconcile import Coverage, ReconcileSession, reconcile_session
from .service import NodeSyncService


_L = getLogger(__name__)


class ListingUnavailableError(Exception):
    """The source could not list a folder now, such as while it is unreachable.

    A pass keeps that folder's subtree as mirrored, as for an ``OSError``. Any
    other exception from listing is a bug and fails the pass.
    """


# What a listing may raise without failing its pass.
_LISTING_FAILURES = (OSError, ListingUnavailableError)


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
        """List *folder*.

        Raises:
            OSError, ListingUnavailableError: The source cannot list it now;
                its subtree is kept as mirrored. Anything else fails the pass.
        """

    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        """Fetch one removal candidate directly (see ``Locate``)."""

    async def skip(self, folder: F) -> bool:
        """Whether *folder* is known unchanged, keeping its subtree.

        Failures are as for ``list_folder``.
        """
        return False

    async def root_finished(self, root: F, complete: bool) -> None:
        """Called once the pass's writes have landed, such as to save progress."""

    async def pass_finished(self, complete: bool) -> None:
        """Called after every ``root_finished``.

        *complete* is true if every root was fully covered and nothing in scope
        was preserved as unusable.
        """


@dataclass(frozen=True, kw_only=True)
class Contents[F]:
    """One folder as the source reported it, for a ``Comparison``."""

    # Every child the listing reported; the core decides which changed.
    children: list[NodeObservation]
    # Folders to visit next.
    subfolders: list[F]


class Comparison[F](ABC):
    """The source-specific part of a comparison pass.

    Unlike a ``Scanner`` pass, which writes what the backend reports as
    changed, a comparison lists every folder in scope and the core compares
    each child with the mirror. Nothing is skipped and no progress is saved, so
    it repairs what checkpoints would miss.
    """

    @abstractmethod
    async def prepare(self) -> ScanPlan[F]:
        """Decide what this pass covers; a failure stops the pass."""

    @abstractmethod
    def folder_id(self, folder: F) -> NodeId:
        """The node of *folder*."""

    @abstractmethod
    async def list_folder(self, folder: F) -> Contents[F]:
        """List *folder*; failures are as for ``Scanner.list_folder``."""

    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        """Fetch one removal candidate directly (see ``Locate``)."""

    async def differs(
        self, pairs: Sequence[tuple[NodeObservation, NodeRecord]]
    ) -> set[NodeId]:
        """Children whose backend state differs from what the mirror holds.

        *pairs* are one folder's listed children with their mirrored records.
        The core compares the records; this covers what it cannot see, such as
        a locator in a backend table.
        """
        return set()


@dataclass(frozen=True, kw_only=True)
class ComparisonReport:
    """What a comparison pass found; in a dry run, what it would change."""

    # Children the listings reported.
    checked: int
    added: int
    updated: int
    # Subtree roots removed; in a dry run, the unconfirmed candidates.
    removed: int
    # Folders that could not be listed, whose subtrees were kept.
    list_errors: int


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

    async def compare[F](
        self, comparison: Comparison[F], *, dry_run: bool = False
    ) -> ComparisonReport:
        """Run one comparison pass of *comparison*, after any pass before it.

        A real run returns once its writes, enrichment included, have landed.

        Raises:
            Exception: What failed in ``prepare``, traversal, or a removal.
        """
        async with self._pass_lock:
            try:
                report = await compare_pass(
                    comparison,
                    mirror=self._mirror,
                    writer=None if dry_run else self._node_sync,
                )
            except Exception as failed:
                await _settle_after(failed, self._node_sync.wait_enrichment_drained)
                raise
            if not dry_run:
                # Removal confirmation queues writes after its own drain.
                await self._node_sync.wait_enrichment_drained()
            return report

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
            writer=self._node_sync,
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
            except _LISTING_FAILURES as e:
                # An expected condition that can repeat on every pass.
                _L.warning(
                    "Cannot list folder %s; keeping its mirror: %s", folder_id, e
                )
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


async def compare_pass[F](
    comparison: Comparison[F],
    *,
    mirror: MirrorReader,
    writer: NodeSyncService | None,
) -> ComparisonReport:
    """Compare everything *comparison* lists with the mirror, and apply it.

    Without a *writer* it is a dry run: it writes nothing, numbers no reads,
    and reports the unconfirmed removal candidates. A real run returns once its
    writes are queued.
    """
    plan = await comparison.prepare()
    if not plan.scope:
        return ComparisonReport(checked=0, added=0, updated=0, removed=0, list_errors=0)
    if writer is None:
        coverage = Coverage(plan.scope, mirror=mirror)
        counts = await _compare_folders(comparison, plan, coverage, mirror, writer)
        roots = await coverage.removal_roots()
        for node_id in roots:
            _L.info("dry-run: would remove %s, absent from the source", node_id)
    else:
        async with reconcile_session(
            plan.scope, mirror=mirror, writer=writer, locate=comparison.locate
        ) as session:
            counts = await _compare_folders(comparison, plan, session, mirror, writer)
            roots = await session.commit_removals()
        for node_id in roots:
            _L.info("Removed node %s, absent from the source", node_id)
    return ComparisonReport(
        checked=counts.checked,
        added=counts.added,
        updated=counts.updated,
        removed=len(roots),
        list_errors=counts.list_errors,
    )


@dataclass(kw_only=True)
class _Counts:
    checked: int = 0
    added: int = 0
    updated: int = 0
    list_errors: int = 0


@dataclass(frozen=True)
class _Writes:
    """Writes from one listing, numbered by its read."""

    writer: NodeSyncService
    read: ReadSeq

    async def inserted(self, observation: NodeObservation) -> None:
        await self.writer.reconcile_insert(observation, read=self.read)

    async def updated(self, observation: NodeObservation, existing: NodeRecord) -> None:
        await self.writer.reconcile_update(observation, existing, read=self.read)


async def _compare_folders[F](
    comparison: Comparison[F],
    plan: ScanPlan[F],
    coverage: Coverage,
    mirror: MirrorReader,
    writer: NodeSyncService | None,
) -> _Counts:
    """List every folder under *plan*, and write or log what differs."""
    counts = _Counts()
    for node_id in plan.preserved:
        coverage.preserve(node_id)

    async def visit(folder: F) -> list[F]:
        if writer is None:
            return await compare(folder, None)
        async with writer.observe() as read:
            return await compare(folder, _Writes(writer, read))

    async def compare(folder: F, writes: _Writes | None) -> list[F]:
        folder_id = comparison.folder_id(folder)
        try:
            contents = await comparison.list_folder(folder)
        except _LISTING_FAILURES as e:
            _L.warning("Cannot list folder %s; keeping its mirror: %s", folder_id, e)
            counts.list_errors += 1
            coverage.preserve(folder_id)
            return []
        children = contents.children
        counts.checked += len(children)
        coverage.saw(o.record.id for o in children)
        mirrored = {r.id: r for r in await mirror.get_children(folder_id)}
        pairs = [
            (o, existing)
            for o in children
            if (existing := mirrored.get(o.record.id)) is not None
        ]
        other = await comparison.differs(pairs) if pairs else set[NodeId]()
        for o in children:
            existing = mirrored.get(o.record.id)
            if existing is None:
                counts.added += 1
                _L.info(
                    "%s missing node %s (%s) under %s",
                    "dry-run: would add" if writes is None else "Adding",
                    o.record.id,
                    o.record.name,
                    folder_id,
                )
                if writes is not None:
                    await writes.inserted(o)
            elif o.record.id in other or _record_differs(existing, o.record):
                counts.updated += 1
                if writes is None:
                    _L.info("dry-run: would update %s (%s)", o.record.id, o.record.name)
                else:
                    await writes.updated(o, existing)
        return contents.subfolders

    await _traverse(plan.roots, visit)
    return counts


def _record_differs(existing: NodeRecord, listed: NodeRecord) -> bool:
    """Whether *listed* changes *existing*, or *existing* lacks its dimensions.

    Dimensions are compared only for being complete: an image or video
    published before it could be probed is probed again.
    """
    if not same_public_metadata(existing, listed):
        return True
    return (listed.is_image or listed.is_video) and (
        existing.width <= 0 or existing.height <= 0
    )


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

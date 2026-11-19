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
from .reconcile import Coverage, reconcile_session
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
            await self._settled(
                run_pass(scanner, plan, mirror=self._mirror, writer=self._node_sync)
            )

    async def compare[F](
        self, comparison: Comparison[F], *, dry_run: bool = False
    ) -> ComparisonReport:
        """Run one comparison pass of *comparison*, after any pass before it.

        A real run returns once its writes, enrichment included, have landed.

        Raises:
            Exception: What failed in ``prepare``, traversal, or a removal.
        """
        async with self._pass_lock:
            return await self._settled(
                compare_pass(
                    comparison,
                    mirror=self._mirror,
                    writer=None if dry_run else self._node_sync,
                )
            )

    async def walk[F](self, lister: FolderLister[F], root: F) -> None:
        """Write everything *lister* lists under *root*, removing nothing.

        Takes no pass lock and calls neither ``skip`` nor the finish hooks.
        Directories land before their children are listed. File enrichment is
        submitted, not awaited.

        Raises:
            Exception: What failed in a listing.
        """
        writer = self._node_sync

        async def visit(folder: F) -> list[F]:
            async with writer.observe() as read:
                listing = await lister.list_folder(folder)
                await _submit(writer, listing, read, land_directories=True)
            return listing.subfolders

        await _traverse([root], visit)

    async def _settled[R](self, work: Awaitable[R]) -> R:
        """*work*'s result; if it fails, what it submitted settles first."""
        try:
            return await work
        except Exception as failed:
            await _settle_after(failed, self._node_sync.wait_enrichment_drained)
            raise


@dataclass(frozen=True, kw_only=True)
class _PassResult:
    # Removed subtree roots; without a writer, the unconfirmed candidates.
    removed: list[NodeId]
    # Folders whose listing failed, whose subtrees were kept.
    list_errors: int


async def run_pass[F](
    scanner: Scanner[F],
    plan: ScanPlan[F],
    *,
    mirror: MirrorReader,
    writer: NodeSyncService | None,
) -> _PassResult:
    """Traverse *plan*, write what *scanner* reports changed, and decide removals.

    With a *writer*, returns once removals, enrichment, and the finish hooks are
    done. Without one it writes nothing, numbers no reads, calls no finish
    hooks, and reports the removal candidates.
    """
    work = [(root, _RootProgress()) for root in plan.roots]
    list_errors = 0

    async def visit(
        coverage: Coverage, item: tuple[F, _RootProgress]
    ) -> list[tuple[F, _RootProgress]]:
        if writer is None:
            return await list_one(coverage, item, None)
        async with writer.observe() as read:
            return await list_one(
                coverage,
                item,
                partial(_submit, writer, read=read, land_directories=False),
            )

    async def list_one(
        coverage: Coverage,
        item: tuple[F, _RootProgress],
        submit: Callable[[Listing[F]], Awaitable[None]] | None,
    ) -> list[tuple[F, _RootProgress]]:
        nonlocal list_errors
        folder, progress = item
        folder_id = scanner.folder_id(folder)
        try:
            if await scanner.skip(folder):
                coverage.preserve(folder_id)
                return []
            listing = await scanner.list_folder(folder)
        except _LISTING_FAILURES as e:
            # An expected condition that can repeat on every pass.
            _L.warning("Cannot list folder %s; keeping its mirror: %s", folder_id, e)
            coverage.preserve(folder_id)
            progress.complete = False
            list_errors += 1
            return []
        coverage.saw(listing.seen)
        for node_id in listing.keep:
            coverage.preserve(node_id)
        if submit is not None:
            await submit(listing)
        return [(child, progress) for child in listing.subfolders]

    async def traverse(coverage: Coverage) -> None:
        for node_id in plan.preserved:
            coverage.preserve(node_id)
        step = partial(visit, coverage)
        if scanner.concurrent_roots:
            await _traverse(work, step)
        else:
            for item in work:
                await _traverse([item], step)

    if writer is None:
        coverage = Coverage(plan.scope, mirror=mirror)
        await traverse(coverage)
        removed = await coverage.removal_roots()
    else:
        async with reconcile_session(
            plan.scope, mirror=mirror, writer=writer, locate=scanner.locate
        ) as session:
            await traverse(session)
            removed = await session.commit_removals()
        # Removal confirmation queues writes after its own drain.
        await writer.wait_enrichment_drained()
        # Progress is saved only for what was written.
        for root, progress in work:
            await scanner.root_finished(root, progress.complete)
        await scanner.pass_finished(
            not plan.preserved and all(p.complete for _, p in work)
        )
    return _PassResult(removed=removed, list_errors=list_errors)


async def _submit[F](
    writer: NodeSyncService,
    listing: Listing[F],
    read: ReadSeq,
    *,
    land_directories: bool,
) -> None:
    directories = [o for o in listing.changed if o.record.is_directory]
    files = [o for o in listing.changed if not o.record.is_directory]
    await writer.upsert_batch(directories, read=read)
    if land_directories and directories:
        await writer.barrier()
    await writer.upsert_file_batch(files, read=read)


async def compare_pass[F](
    comparison: Comparison[F],
    *,
    mirror: MirrorReader,
    writer: NodeSyncService | None,
) -> ComparisonReport:
    """Run *comparison* as a scan pass that compares every child with the mirror.

    Without a *writer* it is a dry run: it writes nothing, numbers no reads,
    and reports the unconfirmed removal candidates.
    """
    plan = await comparison.prepare()
    if not plan.scope:
        return ComparisonReport(checked=0, added=0, updated=0, removed=0, list_errors=0)
    scanner = _Comparing(comparison, mirror=mirror, dry_run=writer is None)
    result = await run_pass(scanner, plan, mirror=mirror, writer=writer)
    for node_id in result.removed:
        if writer is None:
            _L.info("dry-run: would remove %s, absent from the source", node_id)
        else:
            _L.info("Removed node %s, absent from the source", node_id)
    return ComparisonReport(
        checked=scanner.checked,
        added=scanner.added,
        updated=scanner.updated,
        removed=len(result.removed),
        list_errors=result.list_errors,
    )


class _Comparing[F](Scanner[F]):
    """A ``Comparison`` as a scan pass: every listed child that differs changed."""

    def __init__(
        self, comparison: Comparison[F], *, mirror: MirrorReader, dry_run: bool
    ) -> None:
        self._comparison = comparison
        self._mirror = mirror
        self._dry_run = dry_run
        self.checked = 0
        self.added = 0
        self.updated = 0

    async def prepare(self) -> ScanPlan[F]:
        return await self._comparison.prepare()

    def folder_id(self, folder: F) -> NodeId:
        return self._comparison.folder_id(folder)

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        return await self._comparison.locate(node_id)

    async def list_folder(self, folder: F) -> Listing[F]:
        folder_id = self.folder_id(folder)
        contents = await self._comparison.list_folder(folder)
        children = contents.children
        self.checked += len(children)
        try:
            mirrored = {r.id: r for r in await self._mirror.get_children(folder_id)}
            pairs = [
                (o, existing)
                for o in children
                if (existing := mirrored.get(o.record.id)) is not None
            ]
            other = await self._comparison.differs(pairs) if pairs else set[NodeId]()
        except _LISTING_FAILURES as e:
            # Only the source listing may be unavailable; this is not one.
            raise RuntimeError(f"comparing folder {folder_id} failed") from e
        changed: list[NodeObservation] = []
        for o in children:
            existing = mirrored.get(o.record.id)
            if existing is None:
                self.added += 1
                _L.info(
                    "%s missing node %s (%s) under %s",
                    "dry-run: would add" if self._dry_run else "Adding",
                    o.record.id,
                    o.record.name,
                    folder_id,
                )
                changed.append(o)
            elif o.record.id in other or _record_differs(existing, o.record):
                self.updated += 1
                if self._dry_run:
                    _L.info("dry-run: would update %s (%s)", o.record.id, o.record.name)
                changed.append(o)
        return Listing(
            seen=[o.record.id for o in children],
            changed=changed,
            subfolders=contents.subfolders,
            keep=[],
        )


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

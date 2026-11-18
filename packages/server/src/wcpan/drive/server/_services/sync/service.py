"""Central service for persisting node changes from scan, webhook, and API sources."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import copy
from dataclasses import dataclass, replace
from functools import partial
from logging import getLogger
from typing import Concatenate

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..._lib.write_queue import (
    WriteQueue,
    run_queued,
)
from ..._types import (
    NodeObservation,
    ReadSeq,
)
from ..media.enricher import MediaEnrichmentError, MediaEnrichService
from ..media.probe.source import ProbeFailure
from ..media.probing import MediaProbing
from ..media.retries import MediaRetries, RetryMedia
from ..mirror.model import (
    BackendNamespace,
    BackendSchema,
)
from ..mirror.store import (
    MirrorStore,
)
from ..operations import LocateUndecidedError
from .metadata import (
    METADATA_WORKER_COUNT,
    MetadataQueue,
    MetadataWorkItem,
    metadata_worker,
)
from .ordering import ObservationOrder


def _has_complete_media_dims(record: NodeRecord) -> bool:
    """True when *record* already has full media dimensions (no probing needed)."""
    if not (record.is_image or record.is_video):
        return True
    return record.width > 0 and record.height > 0


def _same_content(a: NodeRecord, b: NodeRecord) -> bool:
    """True when *a* and *b* describe the same file content."""
    return a.size == b.size and a.hash == b.hash


def _with_dims_of(record: NodeRecord, source: NodeRecord) -> NodeRecord:
    return replace(
        record,
        width=source.width,
        height=source.height,
    )


def _reusable_dims(existing: NodeRecord | None, record: NodeRecord) -> bool:
    """True when *existing*'s media dimensions still describe *record*.

    The content must be the same, and the dimensions complete for *record*'s
    media kind, which a rename can change.
    """
    return (
        existing is not None
        and _same_content(existing, record)
        and _has_complete_media_dims(_with_dims_of(record, existing))
    )


class WriterStoppedError(RuntimeError):
    """The write worker ended while writes were still waiting to land."""


@dataclass
class _Workers:
    """Whether metadata workers must run, how many scopes run them, and the
    write worker, if the core said which task it is."""

    required: bool
    active: int = 0
    writer: "asyncio.Task[None] | None" = None


type LocateSource = Callable[[str], Awaitable[NodeObservation | None]]
"""Observe the item with a source key now; ``None`` if the backend no longer has
it under a mirrored parent. Raises ``LocateUndecidedError`` when that cannot be
decided."""


_L = getLogger(__name__)


class NodeSyncService:
    """Central gateway for persisting node changes.

    Accepts infrastructure (mirror, queues, media probing) and exposes
    a narrow interface of reusable primitives. Internally handles
    media enrichment, write-queue enqueuing, and mirror method dispatch so
    callers are not aware of those concerns.

    Every write names the backend read it came from (``observe``). A write is
    applied only if no observation from a later-started read was applied to the
    node first, so a slow listing or enrichment cannot overwrite a newer
    observation or resurrect a node removed meanwhile.

    Each backend instance writes through its own view (``for_backend``), which
    tags observations so the mirror binds their locators with that backend's
    schema. Views share the ordering, queues, and enrichment.
    """

    def __init__(
        self,
        *,
        mirror: MirrorStore,
        write_queue: WriteQueue,
        metadata_queue: MetadataQueue,
        probing: MediaProbing,
        retries: MediaRetries | None = None,
        require_workers: bool = False,
        writer: "asyncio.Task[None] | None" = None,
    ) -> None:
        """With *require_workers* (offline), queuing enrichment fails unless
        ``enrichment_scope`` runs the workers, since nothing else would.
        *writer* is the task consuming *write_queue*; cleanup stops waiting
        for queued writes once it has ended."""
        self._mirror = mirror
        # Shared by every view, like the queues.
        self._workers = _Workers(required=require_workers, writer=writer)
        self._write_queue = write_queue
        self._metadata_queue = metadata_queue
        self._order = ObservationOrder()
        self._backend: str | None = None
        self._enricher = MediaEnrichService(probing=probing)
        self._retries = retries or MediaRetries()

    def for_backend(self, name: str) -> "NodeSyncService":
        """A view whose observations are tagged as coming from backend *name*."""
        view = copy(self)
        view._backend = name
        return view

    def _own(self, observation: NodeObservation) -> NodeObservation:
        if self._backend is None or observation.backend is not None:
            return observation
        return replace(observation, backend=self._backend)

    @asynccontextmanager
    async def observe(self) -> AsyncGenerator[ReadSeq, None]:
        """Number a backend read; enter before the read starts.

        Writes derived from the read keep it outstanding until they are applied,
        so they may still be enqueued after the context exits.
        """
        read = self._order.begin()
        try:
            yield read
        finally:
            self._order.release(read)

    async def flush(self) -> None:
        """Wait for all pending write-queue operations to complete.

        Raises:
            WriterStoppedError: The write worker ended first.
        """
        await self._join_writes()

    async def barrier(self) -> None:
        """Wait until every write queued before this call has landed.

        Queues an empty job; the queue is first in, first out, so the job runs
        after them. Unlike ``flush``, writes queued later are not waited for.

        Raises:
            WriterStoppedError: The write worker ended before the writes landed.
        """
        landed = asyncio.ensure_future(run_queued(self._write_queue, _nothing))
        writer = self._workers.writer
        if writer is None:
            await landed
            return
        try:
            await asyncio.wait({landed, writer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not landed.done():
                landed.cancel()
        if not landed.done() or landed.cancelled():
            raise WriterStoppedError("the write worker stopped before writes landed")
        landed.result()

    async def wait_enrichment_drained(self) -> None:
        """After scan or bulk work, wait for metadata worker then write queue to finish."""
        await self._metadata_queue.join()
        await self._join_writes()

    async def _join_writes(self) -> None:
        """Wait until every queued write has landed.

        The write worker marks only landed jobs done, so a failed one keeps
        ``join`` waiting; this stops waiting when the worker has ended.

        Raises:
            WriterStoppedError: The write worker ended before the writes landed.
        """
        writer = self._workers.writer
        if writer is None:
            await self._write_queue.join()
            return
        joined = asyncio.ensure_future(self._write_queue.join())
        try:
            await asyncio.wait({joined, writer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            joined.cancel()
        if not joined.done() or joined.cancelled():
            raise WriterStoppedError("the write worker stopped before writes landed")

    async def process_metadata_item(self, item: MetadataWorkItem) -> None:
        """Run from ``metadata_worker``: enrich then enqueue a sync DB write.

        Owns the item's held read until the write job is queued, and releases it
        on any exit before that, cancellation included.
        """
        try:
            enriched = await self._try_enrich(
                item.observation, force_refresh=item.force_refresh, read=item.read
            )
        except BaseException:
            self._order.release(item.read)
            raise
        if enriched is None:
            self._order.release(item.read)
            return
        await self._put_upserts([(enriched, item.read)])

    @asynccontextmanager
    async def enrichment_scope(self) -> AsyncGenerator[None, None]:
        """Run the metadata workers while entered.

        Used by: offline commands that queue enrichment, entered inside the
        connection their probes read through, so no probe outlives it. On a
        normal exit, queued enrichment and the writes it produces finish first.
        On an error, running probes are cancelled and the error is re-raised;
        queued enrichment and writes are abandoned, for the command to redo
        when run again.
        """
        queue = self._metadata_queue
        failed: BaseException | None = None
        self._workers.active += 1
        try:
            async with asyncio.TaskGroup() as group:
                workers = [
                    group.create_task(
                        metadata_worker(queue, self.process_metadata_item)
                    )
                    for _ in range(METADATA_WORKER_COUNT)
                ]
                try:
                    yield
                    await self.wait_enrichment_drained()
                except BaseException as e:
                    failed = e
                    raise
                finally:
                    for worker in workers:
                        worker.cancel()
        except BaseExceptionGroup as group:
            # The caller's own error alone comes out as itself, not in a group.
            if failed is not None and list(group.exceptions) == [failed]:
                raise failed from failed.__cause__
            raise
        finally:
            self._workers.active -= 1

    def _check_workers(self) -> None:
        if self._workers.required and not self._workers.active:
            raise RuntimeError(
                "enrichment was queued with no metadata workers; a command that"
                " queues enrichment must declare probes and run enrichment_scope"
            )

    async def _hand_off[T](
        self, queue: asyncio.Queue[T], item: T, reads: Iterable[ReadSeq]
    ) -> None:
        """Queue *item*, which takes over the held *reads*.

        Both queues are bounded, so the ``put`` can wait; if it fails or is
        cancelled, nothing owns the reads, so they are released here.
        """
        try:
            if queue is self._metadata_queue:
                self._check_workers()
            await queue.put(item)
        except BaseException:
            for read in reads:
                self._order.release(read)
            raise

    async def _put_upserts(self, items: list[tuple[NodeObservation, ReadSeq]]) -> None:
        """Enqueue a write of *items*; each read must already be held.

        Observations from reads older than the node's latest applied one are
        dropped when the write runs. The reads are released once the write
        runs, or here if it cannot be queued.
        """

        async def write() -> None:
            try:
                admitted = [
                    (o, read)
                    for o, read in items
                    if self._order.admits(o.record.id, read)
                ]
                applied = await self._mirror.apply_observations(
                    [o for o, _ in admitted]
                )
                for o, read in admitted:
                    if o.record.id in applied:
                        self._order.record(o.record.id, read)
                        if o.backend is not None and o.source_key is not None:
                            self._retries.forget(o.backend, o.source_key)
            finally:
                for _, read in items:
                    self._order.release(read)

        await self._hand_off(self._write_queue, write, [read for _, read in items])

    # --- Shared primitives (used by multiple sources) ---

    async def upsert(
        self, observation: NodeObservation, *, read: ReadSeq, strict: bool = False
    ) -> NodeRecord:
        """Async-enrich, then enqueue an ordered write.

        Returns the (possibly enriched) record so callers can use it in responses.
        With *strict*, a failed media probe raises instead of withholding the file.
        Used by: API (create, update, upload), webhook (delayed per-file upsert).

        Raises:
            MediaEnrichmentError: With *strict*, if the media probe fails.
        """
        self._order.hold(read)
        observation = self._own(observation)
        try:
            enriched = await self._try_enrich(
                observation, force_refresh=False, read=read, strict=strict
            )
        except BaseException:
            self._order.release(read)
            raise
        if enriched is None:
            self._order.release(read)
            return observation.record
        await self._put_upserts([(enriched, read)])
        return enriched.record

    async def delete(self, node_id: NodeId, *, read: ReadSeq) -> None:
        """Enqueue a subtree removal unless a later read already observed the node.

        Used by: API (delete node), webhook (file_removed), scan (confirmed absence).
        """
        self._order.hold(read)

        async def remove() -> None:
            try:
                if not self._order.admits(node_id, read):
                    return
                await self._mirror.delete_subtree_and_emit_changes(node_id)
                self._order.record(node_id, read)
            finally:
                self._order.release(read)

        await self._hand_off(self._write_queue, remove, [read])

    # --- Batch primitives (shared by scan + webhook) ---

    async def upsert_batch(
        self, observations: list[NodeObservation], *, read: ReadSeq
    ) -> None:
        """Async-enrich each observation, then enqueue a batch upsert.

        Uses PRESERVE_MEDIA_UPSERT_SQL so existing enriched dimensions are not
        overwritten by a re-scan or webhook update.

        Used by: scan (directory records per BFS level), webhook (batch upserts).
        """
        if not observations:
            return
        for _ in observations:
            self._order.hold(read)
        # One hold per observation; those not yet released are owned here.
        owned = len(observations)
        enriched: list[tuple[NodeObservation, ReadSeq]] = []
        try:
            for o in observations:
                o = self._own(o)
                if (
                    e := await self._try_enrich(o, force_refresh=False, read=read)
                ) is not None:
                    enriched.append((e, read))
                else:
                    self._order.release(read)
                    owned -= 1
        except BaseException:
            for _ in range(owned):
                self._order.release(read)
            raise
        if not enriched:
            return
        await self._put_upserts(enriched)

    async def _try_enrich(
        self,
        observation: NodeObservation,
        *,
        force_refresh: bool,
        read: ReadSeq,
        strict: bool = False,
    ) -> NodeObservation | None:
        """Enrich *observation*, or ``None`` when it is withheld.

        A published file whose content is unchanged keeps its dimensions without
        a probe. When the probe fails, a published file keeps its last probed
        dimensions; a file never published is withheld and its failure recorded.
        """
        record = observation.record
        existing = (
            await self._mirror.get_node_by_id(record.id)
            if record.is_image or record.is_video
            else None
        )
        if existing is not None and _reusable_dims(existing, record):
            return replace(observation, record=_with_dims_of(record, existing))
        if observation.backend is not None and observation.source_key is not None:
            self._retries.attempting(observation.backend, observation.source_key)
        try:
            enriched = await self._enricher.enrich(
                observation, force_refresh=force_refresh
            )
        except MediaEnrichmentError as e:
            if strict:
                raise
            if observation.backend is None:
                # Only backend views observe files; the core's own nodes are
                # directories and never need enrichment.
                raise ValueError(
                    f"Untagged observation of {record.id} needs enrichment"
                ) from e
            if existing is not None:
                _L.warning(
                    "Keeping the last media dimensions of %s (%s); probing failed: %s",
                    record.id,
                    record.name,
                    e.reason,
                )
                return observation
            _L.warning(
                "Withholding %s (%s); probing failed: %s",
                record.id,
                record.name,
                e.reason,
            )
            await self._mark_media_failure(observation, e.kind, read=read)
            return None
        return replace(observation, record=enriched)

    async def _mark_media_failure(
        self, observation: NodeObservation, kind: ProbeFailure, *, read: ReadSeq
    ) -> None:
        """Record the failure unless a later read already observed the node.

        A transient failure is retried with backoff while the backend serves.
        """
        backend = observation.backend
        source_key = observation.source_key
        if backend is None or source_key is None:
            _L.warning(
                "%s has no source key; its media failure is not recorded",
                observation.record.id,
            )
            return
        node_id = observation.record.id
        self._order.hold(read)

        async def mark() -> None:
            try:
                if self._order.admits(node_id, read):
                    await self._mirror.mark_media_failure(backend, source_key, kind)
                    # The failure is what this read observed; an older read's
                    # result must not apply over it.
                    self._order.record(node_id, read)
                    if kind == ProbeFailure.TRANSIENT:
                        self._retries.failed(backend, source_key)
                    else:
                        self._retries.forget(backend, source_key)
            finally:
                self._order.release(read)

        await self._hand_off(self._write_queue, mark, [read])

    async def upsert_file_batch(
        self, observations: list[NodeObservation], *, read: ReadSeq
    ) -> None:
        """After directory rows are committed, enqueue per-file media enrichment.

        Waits for prior write-queue work so ``get_ancestors`` in the metadata worker
        sees parent rows from the preceding ``upsert_batch`` for this BFS level.

        Records whose existing DB row already has complete media dimensions for
        the same content are written directly, skipping the metadata queue.
        """
        if not observations:
            return
        observations = [self._own(o) for o in observations]
        for _ in observations:
            self._order.hold(read)
        # One hold per observation; those not yet handed off are owned here.
        owned = len(observations)
        mq = self._metadata_queue
        skip_batch: list[tuple[NodeObservation, ReadSeq]] = []
        try:
            await self._join_writes()

            node_ids = [o.record.id for o in observations]
            existing = await self._mirror.get_nodes_by_ids(node_ids)

            for o in observations:
                db_rec = existing.get(o.record.id)
                if _reusable_dims(db_rec, o.record):
                    skip_batch.append((o, read))
                else:
                    self._check_workers()
                    await mq.put(
                        MetadataWorkItem(observation=o, force_refresh=True, read=read)
                    )
                    owned -= 1
        except BaseException:
            for _ in range(owned):
                self._order.release(read)
            raise

        if skip_batch:
            await self._put_upserts(skip_batch)

    async def retry_media_failures(self, *, locate: LocateSource) -> None:
        """Observe the files whose media probe failed again, and probe them.

        Only this view's backend records are retried, whatever their kind, and
        not those a backoff retry already covers. A record whose file the
        backend no longer has is dropped.
        Used by: backends whose discovery would not see an unchanged file again,
        once at startup.
        """
        backend = self._require_backend()
        pending = self._retries.pending(backend)
        for failure in await self._mirror.get_media_failures(backend):
            if failure.source_key in pending:
                continue
            try:
                await self._reprobe(failure.source_key, locate=locate)
            except LocateUndecidedError:
                _L.warning(
                    "Could not re-observe %s; retrying later", failure.source_key
                )

    def retrying(self, locate: LocateSource) -> AbstractAsyncContextManager[None]:
        """Retry this view's transient media failures with backoff while entered.

        Each attempt re-observes the file with *locate* under a fresh read.
        Used by: backends while serving.
        """
        return self.retrying_with(partial(self._reprobe, locate=locate))

    def retrying_with(self, retry: RetryMedia) -> AbstractAsyncContextManager[None]:
        """Like ``retrying``, for a backend that re-observes files its own way."""
        return self._retries.register(self._require_backend(), retry)

    async def _reprobe(self, source_key: str, *, locate: LocateSource) -> None:
        """Observe the file with *source_key* again and submit it for probing.

        Raises:
            LocateUndecidedError: When the backend cannot tell now.
        """
        backend = self._require_backend()
        async with self.observe() as read:
            current = await locate(source_key)
            if current is None:
                self._retries.forget(backend, source_key)
                await self._write_queue.put(
                    partial(self._mirror.clear_media_failure, backend, source_key)
                )
                return
            self._order.hold(read)
            await self._hand_off(
                self._metadata_queue,
                MetadataWorkItem(
                    observation=self._own(current), force_refresh=True, read=read
                ),
                [read],
            )

    def _require_backend(self) -> str:
        if self._backend is None:
            raise ValueError("Only a backend view retries media failures")
        return self._backend

    async def prune_media_failures(self, *, before: int) -> int:
        """Drop this view's failure records last marked before *before* (ms).

        Used by: backends that probe every unpublished file again on each full
        pass, after the pass and its enrichment finish.
        """
        backend = self._require_backend()

        async def prune() -> int:
            removed = await self._mirror.prune_media_failures(backend, before=before)
            remaining = await self._mirror.get_media_failures(backend)
            self._retries.retain(backend, {f.source_key for f in remaining})
            return removed

        return await run_queued(self._write_queue, prune)

    # --- Reconcile / backfill ---

    async def reconcile_insert(
        self, from_api: NodeObservation, *, read: ReadSeq
    ) -> None:
        self._order.hold(read)
        from_api = self._own(from_api)
        if from_api.record.is_directory:
            # Write in listing order so children never arrive before their parent.
            await self._put_upserts([(from_api, read)])
            return
        await self._hand_off(
            self._metadata_queue,
            MetadataWorkItem(observation=from_api, force_refresh=True, read=read),
            [read],
        )

    async def reconcile_update(
        self, from_api: NodeObservation, existing: NodeRecord, *, read: ReadSeq
    ) -> None:
        self._order.hold(read)
        from_api = self._own(from_api)
        if _reusable_dims(existing, from_api.record):
            await self._put_upserts([(from_api, read)])
            return
        await self._hand_off(
            self._metadata_queue,
            MetadataWorkItem(observation=from_api, force_refresh=True, read=read),
            [read],
        )

    async def register_schema(self, schema: BackendSchema) -> BackendNamespace:
        """Register this view's backend with *schema*, in order with queued writes.

        Used by: backend factories, before they observe anything.
        """
        backend = self._backend
        if backend is None:
            raise ValueError("register_schema needs a backend view")

        async def register() -> BackendNamespace:
            return await self._mirror.register_schema(backend, schema)

        return await run_queued(self._write_queue, register)

    async def enqueue(self, job: Callable[[], Awaitable[None]]) -> None:
        """Run a backend write in order with queued mirror writes.

        Used by: backends, to persist discovery checkpoints after the mirror
        writes they cover.
        """
        await self._write_queue.put(job)

    async def enqueue_sql[**A](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], object],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> None:
        """Queue ``fn(con, ...)`` as one transaction, in order with mirror writes."""

        async def job() -> None:
            await self._mirror.write(fn, *args, **kwargs)

        await self.enqueue(job)

    async def touched_after(self, read: ReadSeq) -> set[NodeId]:
        """Nodes observed by reads that started after *read*, once queued writes land.

        Used by: scanner, so end-of-scan removals skip nodes seen by newer reads.
        """
        await self.wait_enrichment_drained()
        return self._order.touched_after(read)


async def _nothing() -> None:
    """The empty job ``barrier`` queues."""

"""Central service for persisting node changes from scan, webhook, and API sources."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import copy
from dataclasses import replace
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
    ProbeFailure,
    ReadSeq,
)
from ..media.probing import MediaProbing
from ..media.retries import MediaRetries
from ..mirror.model import (
    BackendNamespace,
    BackendSchema,
    MediaFailure,
)
from ..mirror.store import (
    MirrorStore,
)
from .metadata import MetadataQueue, MetadataWorkItem
from .ordering import ObservationOrder
from .prober import (
    Outcome,
    ProbeFailed,
    Prober,
    is_media,
    needs_probe,
    observation_of,
    with_dims_of,
)


class WriterStoppedError(RuntimeError):
    """The write worker ended while writes were still waiting to land."""


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
        self._writer = writer
        self._write_queue = write_queue
        self._metadata_queue = metadata_queue
        self._order = ObservationOrder()
        self._backend: str | None = None
        self._retries = retries or MediaRetries()
        self._prober = Prober(
            mirror=mirror,
            probing=probing,
            retries=self._retries,
            metadata_queue=metadata_queue,
            require_workers=require_workers,
        )

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
        writer = self._writer
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
        writer = self._writer
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
            outcome = await self._prober.outcome(
                item.observation, force_refresh=item.force_refresh
            )
        except BaseException:
            self._order.release(item.read)
            raise
        await self._put_outcomes([(outcome, item.read)])

    def enrichment_scope(self) -> AbstractAsyncContextManager[None]:
        """Run the metadata workers while entered; see ``Prober.scope``.

        Used by: offline commands that queue enrichment, entered inside the
        connection their probes read through, so no probe outlives it.
        """
        return self._prober.scope(
            self.process_metadata_item, drain=self.wait_enrichment_drained
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
                self._prober.check_workers()
            await queue.put(item)
        except BaseException:
            for read in reads:
                self._order.release(read)
            raise

    async def _put_outcomes(self, items: list[tuple[Outcome, ReadSeq]]) -> None:
        """Enqueue a write of *items*; each read must already be held.

        Outcomes from reads older than the node's latest applied one are dropped
        when the write runs. A failed probe is decided there too, against the
        mirror as it is then: a published file keeps its stored dimensions,
        and any other is withheld with a failure record. The reads are released
        once the write runs, or here if it cannot be queued.
        """

        async def write() -> None:
            try:
                publish: list[tuple[NodeObservation, ReadSeq]] = []
                for outcome, read in items:
                    observation = observation_of(outcome)
                    if not self._order.admits(observation.record.id, read):
                        continue
                    if not isinstance(outcome, ProbeFailed):
                        publish.append((outcome, read))
                        continue
                    kept = await self._keep_or_withhold(outcome, read)
                    if kept is not None:
                        publish.append((kept, read))
                if not publish:
                    return
                applied = await self._mirror.apply_observations([o for o, _ in publish])
                for o, read in publish:
                    if o.record.id in applied:
                        self._order.record(o.record.id, read)
                        if o.backend is not None and o.source_key is not None:
                            self._retries.forget(o.backend, o.source_key)
            finally:
                for _, read in items:
                    self._order.release(read)

        await self._hand_off(self._write_queue, write, [read for _, read in items])

    async def _keep_or_withhold(
        self, failed: ProbeFailed, read: ReadSeq
    ) -> NodeObservation | None:
        """From the write job: what an admitted failed probe publishes.

        A file in the mirror is published with its stored dimensions, whatever
        the observation carried. Any other is withheld: its failure is recorded
        under the backend's source key, and ``None`` is returned.
        """
        observation = failed.observation
        record = observation.record
        existing = await self._mirror.get_node_by_id(record.id)
        if existing is not None:
            _L.warning(
                "Keeping the last media dimensions of %s (%s); probing failed: %s",
                record.id,
                record.name,
                failed.reason,
            )
            return replace(observation, record=with_dims_of(record, existing))
        _L.warning(
            "Withholding %s (%s); probing failed: %s",
            record.id,
            record.name,
            failed.reason,
        )
        backend = observation.backend
        source_key = observation.source_key
        if backend is None or source_key is None:
            _L.warning(
                "%s has no source key; its media failure is not recorded", record.id
            )
            return None
        await self._mirror.mark_media_failure(backend, source_key, failed.kind)
        # The failure is what this read observed; an older read's result must
        # not apply over it.
        self._order.record(record.id, read)
        if failed.kind == ProbeFailure.TRANSIENT:
            self._retries.failed(backend, source_key)
        else:
            self._retries.forget(backend, source_key)
        return None

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
            outcome = await self._prober.outcome(
                observation, force_refresh=False, strict=strict
            )
        except BaseException:
            self._order.release(read)
            raise
        await self._put_outcomes([(outcome, read)])
        return observation_of(outcome).record

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
        outcomes: list[tuple[Outcome, ReadSeq]] = []
        try:
            for o in observations:
                outcome = await self._prober.outcome(self._own(o), force_refresh=False)
                outcomes.append((outcome, read))
        except BaseException:
            for _ in observations:
                self._order.release(read)
            raise
        await self._put_outcomes(outcomes)

    async def upsert_file_batch(
        self, observations: list[NodeObservation], *, read: ReadSeq
    ) -> None:
        """Queue media that needs a probe; write every other file directly.

        Only an image or video whose stored dimensions do not apply is probed;
        other files, and media keeping its dimensions, are written as observed.

        First waits for the writes queued before this call, not later ones, so
        the stored records it reads, here and in the metadata worker, include
        every publication queued before it. Otherwise a file published by an
        older read but not yet written would look unpublished, and a failed
        probe would withhold it instead of keeping its dimensions.
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
            await self.barrier()

            media_ids = [o.record.id for o in observations if is_media(o.record)]
            existing = (
                await self._mirror.get_nodes_by_ids(media_ids) if media_ids else {}
            )

            for o in observations:
                if not needs_probe(existing.get(o.record.id), o.record):
                    skip_batch.append((o, read))
                else:
                    self._prober.check_workers()
                    await mq.put(
                        MetadataWorkItem(observation=o, force_refresh=True, read=read)
                    )
                    owned -= 1
        except BaseException:
            for _ in range(owned):
                self._order.release(read)
            raise

        if skip_batch:
            await self._put_outcomes(list(skip_batch))

    @property
    def retries(self) -> MediaRetries:
        """Retry timers of media probes, shared by every view."""
        return self._retries

    @property
    def backend(self) -> str:
        """The backend this view writes for.

        Raises:
            ValueError: For the core's own view, which has none.
        """
        return self._require_backend()

    async def media_failures(self) -> list[MediaFailure]:
        """This view's recorded media failures."""
        return await self._mirror.get_media_failures(self._require_backend())

    async def submit_probe(
        self, observation: NodeObservation, *, read: ReadSeq
    ) -> None:
        """Queue *observation* for probing again, as observed by *read*.

        Used by: media retries, after re-observing a file whose probe failed.
        """
        self._order.hold(read)
        await self._hand_off(
            self._metadata_queue,
            MetadataWorkItem(
                observation=self._own(observation), force_refresh=True, read=read
            ),
            [read],
        )

    async def drop_media_failure(self, source_key: str) -> None:
        """Forget the failure recorded under *source_key*; its file is gone."""
        backend = self._require_backend()
        self._retries.forget(backend, source_key)
        await self._write_queue.put(
            partial(self._mirror.clear_media_failure, backend, source_key)
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

"""Central service for persisting node changes from scan, webhook, and API sources."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import replace
from logging import getLogger

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord

from ..types import (
    MetadataQueue,
    MetadataWorkItem,
    NodeObservation,
    ReadSeq,
    WriteQueue,
)
from .enricher import MediaEnrichmentError, MediaEnrichService
from .off_main import OffMainService
from .operations import Locate, LocateUndecidedError
from .ordering import ObservationOrder
from .paths import LocalPathService, MountSource
from .storage import StorageService


def _has_complete_media_dims(record: NodeRecord) -> bool:
    """True when *record* already has full media dimensions (no probing needed)."""
    if not (record.is_image or record.is_video):
        return True
    if record.width > 0 and record.height > 0:
        if record.is_video:
            return record.ms_duration > 0
        return True
    return False


_L = getLogger(__name__)


class NodeSyncService:
    """Central gateway for persisting node changes.

    Accepts infrastructure (storage, write_queue, off_main, mounts) and exposes
    a narrow interface of reusable primitives. Internally handles
    media enrichment, write-queue enqueuing, and storage method dispatch so
    callers are not aware of those concerns.

    Every write names the backend read it came from (``observe``). A write is
    applied only if no observation from a later-started read was applied to the
    node first, so a slow listing or enrichment cannot overwrite a newer
    observation or resurrect a node removed meanwhile.

    Each backend instance writes through its own view (``for_backend``), which
    tags observations so storage binds their locators with that backend's
    schema. Views share the ordering, queues, and enrichment.
    """

    def __init__(
        self,
        *,
        storage: StorageService,
        write_queue: WriteQueue,
        off_main: OffMainService,
        mounts: Mapping[str, MountSource],
        metadata_queue: MetadataQueue,
    ) -> None:
        self._storage = storage
        self._write_queue = write_queue
        self._metadata_queue = metadata_queue
        self._order = ObservationOrder()
        self._backend: str | None = None
        local_path_service = LocalPathService(storage=storage, mounts=mounts)
        self._enricher = MediaEnrichService(
            local_path_service=local_path_service,
            off_main=off_main,
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
        """Wait for all pending write-queue operations to complete."""
        await self._write_queue.join()

    async def wait_enrichment_drained(self) -> None:
        """After scan or bulk work, wait for metadata worker then write queue to finish."""
        await self._metadata_queue.join()
        await self._write_queue.join()

    async def process_metadata_item(self, item: MetadataWorkItem) -> None:
        """Run from ``metadata_worker``: enrich then enqueue a sync DB write."""
        enriched = await self._try_enrich(
            item.observation, force_refresh=item.force_refresh, read=item.read
        )
        if enriched is None:
            self._order.release(item.read)
            return
        await self._put_upserts([(enriched, item.read)])

    async def _put_upserts(self, items: list[tuple[NodeObservation, ReadSeq]]) -> None:
        """Enqueue a write of *items*; each read must already be held.

        Observations from reads older than the node's latest applied one are
        dropped when the write runs.
        """

        async def write() -> None:
            try:
                admitted = [
                    (o, read)
                    for o, read in items
                    if self._order.admits(o.record.id, read)
                ]
                applied = await self._storage.apply_observations(
                    [o for o, _ in admitted]
                )
                for o, read in admitted:
                    if o.record.id in applied:
                        self._order.record(o.record.id, read)
            finally:
                for _, read in items:
                    self._order.release(read)

        await self._write_queue.put(write)

    # --- Shared primitives (used by multiple sources) ---

    async def upsert(
        self, observation: NodeObservation, *, read: ReadSeq
    ) -> NodeRecord:
        """Async-enrich, then enqueue an ordered write.

        Returns the (possibly enriched) record so callers can use it in responses.
        Used by: API (create, update, upload), webhook (delayed per-file upsert).
        """
        self._order.hold(read)
        observation = self._own(observation)
        enriched = await self._try_enrich(observation, force_refresh=False, read=read)
        if enriched is None:
            self._order.release(read)
            return observation.record
        await self._put_upserts([(enriched, read)])
        return enriched.record

    async def delete(self, node_id: MirrorStableId, *, read: ReadSeq) -> None:
        """Enqueue a subtree removal unless a later read already observed the node.

        Used by: API (delete node), webhook (file_removed), scan (confirmed absence).
        """
        self._order.hold(read)

        async def remove() -> None:
            try:
                if not self._order.admits(node_id, read):
                    return
                await self._storage.delete_subtree_and_emit_changes(node_id)
                self._order.record(node_id, read)
            finally:
                self._order.release(read)

        await self._write_queue.put(remove)

    async def enrich(self, record: NodeRecord) -> NodeRecord:
        """Async media enrichment only — no persistence side-effects.

        Used by: webhook, which enriches before deciding the operation type.
        """
        return await self._enricher.enrich(record, force_refresh=False)

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
        enriched: list[tuple[NodeObservation, ReadSeq]] = []
        for o in observations:
            o = self._own(o)
            if (
                e := await self._try_enrich(o, force_refresh=False, read=read)
            ) is not None:
                enriched.append((e, read))
            else:
                self._order.release(read)
        if not enriched:
            return
        await self._put_upserts(enriched)

    async def _try_enrich(
        self, observation: NodeObservation, *, force_refresh: bool, read: ReadSeq
    ) -> NodeObservation | None:
        """Enrich *observation*; on failure withhold it and record durable retry work."""
        record = observation.record
        try:
            enriched = await self._enricher.enrich(record, force_refresh=force_refresh)
        except MediaEnrichmentError as e:
            _L.warning(
                "Withholding node %s (%s) after media enrichment failed at %s",
                record.id,
                record.name,
                e.path,
            )
            await self._mark_enrichment_pending(
                record.id, observation.backend, read=read
            )
            return None
        return replace(observation, record=enriched)

    async def _mark_enrichment_pending(
        self, node_id: MirrorStableId, backend: str | None, *, read: ReadSeq
    ) -> None:
        """Persist retry work unless a later read already observed the node."""
        self._order.hold(read)

        async def mark() -> None:
            try:
                if self._order.admits(node_id, read):
                    await self._storage.mark_enrichment_pending(node_id, backend)
            finally:
                self._order.release(read)

        await self._write_queue.put(mark)

    async def upsert_file_batch(
        self, observations: list[NodeObservation], *, read: ReadSeq
    ) -> None:
        """After directory rows are committed, enqueue per-file media enrichment.

        Waits for prior write-queue work so ``get_ancestors`` in the metadata worker
        sees parent rows from the preceding ``upsert_batch`` for this BFS level.

        Records whose existing DB row already has complete media dimensions are
        written directly, skipping the expensive metadata queue.
        """
        if not observations:
            return
        observations = [self._own(o) for o in observations]
        for _ in observations:
            self._order.hold(read)
        mq = self._metadata_queue
        await self._write_queue.join()

        node_ids = [o.record.id for o in observations]
        existing = await self._storage.get_nodes_by_ids(node_ids)

        skip_batch: list[tuple[NodeObservation, ReadSeq]] = []
        for o in observations:
            db_rec = existing.get(o.record.id)
            if db_rec is not None and _has_complete_media_dims(db_rec):
                skip_batch.append((o, read))
            else:
                await mq.put(
                    MetadataWorkItem(observation=o, force_refresh=True, read=read)
                )

        if skip_batch:
            await self._put_upserts(skip_batch)

    async def retry_pending_enrichment(self, *, locate: Locate) -> None:
        """Re-observe nodes whose enrichment failed and enqueue them again.

        Only this view's backend entries are retried. A node the backend no
        longer has is removed, which also clears its entry.
        Used by: scanner, after the startup scan.
        """
        for node_id in await self._storage.get_pending_enrichment(self._backend):
            async with self.observe() as read:
                try:
                    current = await locate(node_id)
                except LocateUndecidedError:
                    _L.warning("Could not re-observe %s; retrying later", node_id)
                    continue
                if current is None:
                    await self.delete(node_id, read=read)
                    continue
                self._order.hold(read)
                await self._metadata_queue.put(
                    MetadataWorkItem(
                        observation=self._own(current), force_refresh=True, read=read
                    )
                )

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
        await self._metadata_queue.put(
            MetadataWorkItem(observation=from_api, force_refresh=True, read=read)
        )

    async def reconcile_update(
        self, from_api: NodeObservation, existing: NodeRecord, *, read: ReadSeq
    ) -> None:
        self._order.hold(read)
        from_api = self._own(from_api)
        if _has_complete_media_dims(existing):
            await self._put_upserts([(from_api, read)])
            return
        await self._metadata_queue.put(
            MetadataWorkItem(observation=from_api, force_refresh=True, read=read)
        )

    async def enqueue(self, job: Callable[[], Awaitable[None]]) -> None:
        """Run a backend write in order with queued mirror writes.

        Used by: backends, to persist discovery checkpoints after the mirror
        writes they cover.
        """
        await self._write_queue.put(job)

    async def touched_after(self, read: ReadSeq) -> set[MirrorStableId]:
        """Nodes observed by reads that started after *read*, once queued writes land.

        Used by: scanner, so end-of-scan removals skip nodes seen by newer reads.
        """
        await self.wait_enrichment_drained()
        return self._order.touched_after(read)

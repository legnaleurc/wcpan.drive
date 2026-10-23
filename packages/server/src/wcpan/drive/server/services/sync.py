"""Central service for persisting node changes from scan, webhook, and API sources."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.synology import SynologyPath

from ..types import (
    MetadataQueue,
    MetadataWorkItem,
    NodeObservation,
    ReadSeq,
    WriteQueue,
)
from .enricher import MediaEnrichmentError, MediaEnrichService
from .off_main import OffMainService
from .ordering import ObservationOrder
from .paths import LocalPathService
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

    Accepts infrastructure (storage, write_queue, off_main, mounts, local_paths)
    and exposes a narrow interface of reusable primitives. Internally handles
    media enrichment, write-queue enqueuing, and storage method dispatch so
    callers are not aware of those concerns.

    Every write names the backend read it came from (``observe``). A write is
    applied only if no observation from a later-started read was applied to the
    node first, so a slow listing or enrichment cannot overwrite a newer
    observation or resurrect a node removed meanwhile.
    """

    def __init__(
        self,
        *,
        storage: StorageService,
        write_queue: WriteQueue,
        off_main: OffMainService,
        mounts: dict[str, SynologyPath],
        local_paths: dict[str, str],
        metadata_queue: MetadataQueue,
    ) -> None:
        self._storage = storage
        self._write_queue = write_queue
        self._mounts = mounts
        self._local_paths = local_paths
        self._metadata_queue = metadata_queue
        self._order = ObservationOrder()
        local_path_service = LocalPathService(
            storage=storage,
            mounts=mounts,
            local_paths=local_paths,
        )
        self._enricher = MediaEnrichService(
            local_path_service=local_path_service,
            off_main=off_main,
        )

    @property
    def local_paths(self) -> dict[str, str]:
        return self._local_paths

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
            item.observation, force_refresh=item.force_refresh
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
        enriched = await self._try_enrich(observation, force_refresh=False)
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

    async def remove_listed_child(
        self, node_id: MirrorStableId, parent_id: MirrorStableId, *, read: ReadSeq
    ) -> None:
        """Delete *node_id* only if it is still a child of *parent_id* when applied.

        A listing that no longer contains a node does not prove deletion: the node
        may have moved after the mirror was read, or a later read may have seen it.

        Used by: backfill.
        """
        self._order.hold(read)

        async def remove() -> None:
            try:
                if not self._order.admits(node_id, read):
                    return
                if await self._storage.delete_subtree_if_parent(node_id, parent_id):
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
            if (e := await self._try_enrich(o, force_refresh=False)) is not None:
                enriched.append((e, read))
            else:
                self._order.release(read)
        if not enriched:
            return
        await self._put_upserts(enriched)

    async def _try_enrich(
        self, observation: NodeObservation, *, force_refresh: bool
    ) -> NodeObservation | None:
        record = observation.record
        try:
            enriched = await self._enricher.enrich(record, force_refresh=force_refresh)
        except MediaEnrichmentError as e:
            _L.warning(
                "Skipping node %s (%s) after media enrichment failed at %s",
                record.id,
                record.name,
                e.path,
            )
            return None
        return replace(observation, record=enriched)

    async def upsert_file_batch(
        self, observations: list[NodeObservation], *, read: ReadSeq
    ) -> None:
        """After directory rows are committed, enqueue per-file media enrichment.

        Waits for prior write-queue work so ``get_ancestors`` in the metadata worker
        sees parent rows from the preceding ``upsert_batch`` for this BFS level.
        When ``local_paths`` is unset, applies files directly without probing.

        Records whose existing DB row already has complete media dimensions are
        written directly, skipping the expensive metadata queue.
        """
        if not observations:
            return
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

    async def sync_nodes(self, records: list[NodeRecord]) -> None:
        """Enqueue bulk_upsert_nodes — upserts without emitting change rows.

        Used by: scanner on resume, to keep structural nodes (root, mounts) in sync
        with config without duplicating change-feed entries from the first run.
        """
        if not records:
            return
        await self._write_queue.put(partial(self._storage.bulk_upsert_nodes, records))

    # --- Reconcile / backfill ---

    async def reconcile_insert(
        self, from_api: NodeObservation, *, read: ReadSeq
    ) -> None:
        self._order.hold(read)
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
        if _has_complete_media_dims(existing):
            await self._put_upserts([(from_api, read)])
            return
        await self._metadata_queue.put(
            MetadataWorkItem(observation=from_api, force_refresh=True, read=read)
        )

    # --- Scan-specific lifecycle operations ---

    async def set_mount_watermark(
        self, name: str, path: SynologyPath, checkpoint: int
    ) -> None:
        """Enqueue set_mount_state. Used by scanner to persist sync_id checkpoints."""
        await self._write_queue.put(
            partial(self._storage.set_mount_state, name, path.path, checkpoint)
        )

    async def touched_after(self, read: ReadSeq) -> set[MirrorStableId]:
        """Nodes observed by reads that started after *read*, once queued writes land.

        Used by: scanner, so end-of-scan removals skip nodes seen by newer reads.
        """
        await self.wait_enrichment_drained()
        return self._order.touched_after(read)

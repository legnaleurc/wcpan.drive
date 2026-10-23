"""Central service for persisting node changes from scan, webhook, and API sources."""

from functools import partial
from logging import getLogger

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.synology import SynologyPath

from ..types import MetadataQueue, MetadataWorkItem, WriteQueue
from .enricher import MediaEnrichmentError, MediaEnrichService
from .off_main import OffMainService
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

    async def flush(self) -> None:
        """Wait for all pending write-queue operations to complete."""
        await self._write_queue.join()

    async def wait_enrichment_drained(self) -> None:
        """After scan or bulk work, wait for metadata worker then write queue to finish."""
        await self._metadata_queue.join()
        await self._write_queue.join()

    async def process_metadata_item(self, item: MetadataWorkItem) -> None:
        """Run from ``metadata_worker``: enrich then enqueue a sync DB write."""
        enriched = await self._try_enrich(item.record, force_refresh=item.force_refresh)
        if enriched is None:
            return
        await self._write_queue.put(
            partial(self._storage.upsert_node_and_emit_change, enriched)
        )

    # --- Shared primitives (used by multiple sources) ---

    async def upsert(self, record: NodeRecord) -> NodeRecord:
        """Async-enrich, then enqueue upsert_node_and_emit_change.

        Returns the (possibly enriched) record so callers can use it in responses.
        Used by: API (create, update, upload), webhook (delayed per-file upsert).
        """
        enriched = await self._try_enrich(record, force_refresh=False)
        if enriched is None:
            return record
        await self._write_queue.put(
            partial(self._storage.upsert_node_and_emit_change, enriched)
        )
        return enriched

    async def delete(self, node_id: MirrorStableId) -> None:
        """Enqueue delete_subtree_and_emit_changes.

        Used by: API (delete node), webhook (file_removed).
        """
        await self._write_queue.put(
            partial(self._storage.delete_subtree_and_emit_changes, node_id)
        )

    async def enrich(self, record: NodeRecord) -> NodeRecord:
        """Async media enrichment only — no persistence side-effects.

        Used by: webhook, which enriches before deciding the operation type.
        """
        return await self._enricher.enrich(record, force_refresh=False)

    # --- Batch primitives (shared by scan + webhook) ---

    async def upsert_batch(self, records: list[NodeRecord]) -> None:
        """Async-enrich each record, then enqueue a batch upsert.

        Uses PRESERVE_MEDIA_UPSERT_SQL so existing enriched dimensions are not
        overwritten by a re-scan or webhook update.

        Used by: scan (directory records per BFS level), webhook (batch upserts).
        """
        if not records:
            return
        enriched: list[NodeRecord] = []
        for r in records:
            if (record := await self._try_enrich(r, force_refresh=False)) is not None:
                enriched.append(record)
        if not enriched:
            return
        await self._write_queue.put(
            partial(self._storage.apply_scan_folder_batch, [], enriched)
        )

    async def _try_enrich(
        self, record: NodeRecord, *, force_refresh: bool
    ) -> NodeRecord | None:
        try:
            return await self._enricher.enrich(record, force_refresh=force_refresh)
        except MediaEnrichmentError as e:
            _L.warning(
                "Skipping node %s (%s) after media enrichment failed at %s",
                record.id,
                record.name,
                e.path,
            )
            return None

    async def upsert_file_batch(self, records: list[NodeRecord]) -> None:
        """After directory rows are committed, enqueue per-file media enrichment.

        Waits for prior write-queue work so ``get_ancestors`` in the metadata worker
        sees parent rows from the preceding ``upsert_batch`` for this BFS level.
        When ``local_paths`` is unset, applies files directly without probing.

        Records whose existing DB row already has complete media dimensions are
        written directly, skipping the expensive metadata queue.
        """
        if not records:
            return
        mq = self._metadata_queue
        await self._write_queue.join()

        node_ids = [r.id for r in records]
        existing = await self._storage.get_nodes_by_ids(node_ids)

        skip_batch: list[NodeRecord] = []
        for r in records:
            db_rec = existing.get(r.id)
            if db_rec is not None and _has_complete_media_dims(db_rec):
                skip_batch.append(r)
            else:
                await mq.put(MetadataWorkItem(record=r, force_refresh=True))

        if skip_batch:
            await self._write_queue.put(
                partial(self._storage.apply_scan_folder_batch, [], skip_batch)
            )

    async def sync_nodes(self, records: list[NodeRecord]) -> None:
        """Enqueue bulk_upsert_nodes — upserts without emitting change rows.

        Used by: scanner on resume, to keep structural nodes (root, mounts) in sync
        with config without duplicating change-feed entries from the first run.
        """
        if not records:
            return
        await self._write_queue.put(partial(self._storage.bulk_upsert_nodes, records))

    # --- Reconcile / backfill ---

    async def reconcile_insert(self, from_api: NodeRecord) -> None:
        await self._metadata_queue.put(
            MetadataWorkItem(record=from_api, force_refresh=True)
        )

    async def reconcile_update(
        self, from_api: NodeRecord, existing: NodeRecord
    ) -> None:
        if _has_complete_media_dims(existing):
            await self._write_queue.put(
                partial(self._storage.upsert_node_and_emit_change, from_api)
            )
            return
        await self._metadata_queue.put(
            MetadataWorkItem(record=from_api, force_refresh=True)
        )

    # --- Scan-specific lifecycle operations ---

    async def set_mount_watermark(
        self, name: str, path: SynologyPath, checkpoint: int
    ) -> None:
        """Enqueue set_mount_state. Used by scanner to persist sync_id checkpoints."""
        await self._write_queue.put(
            partial(self._storage.set_mount_state, name, path.path, checkpoint)
        )

    async def apply_deferred_removals(
        self,
        preserved: set[MirrorStableId],
        mount_ids: set[MirrorStableId],
    ) -> None:
        """Enqueue apply_deferred_scan_removals. Used by scanner for end-of-scan cleanup."""
        await self._write_queue.put(
            partial(self._storage.apply_deferred_scan_removals, preserved, mount_ids)
        )

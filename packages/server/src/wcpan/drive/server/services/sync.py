"""Central service for persisting node changes from scan, webhook, and API sources."""

from dataclasses import replace
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.synology import SynologyPath

from ..types import MetadataQueue, MetadataWorkItem, NodeObservation, WriteQueue
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

    Each observation claims its node when accepted, before any enrichment. A write
    is applied only while its claim is still the latest one, so a slow enrichment
    cannot overwrite a newer observation or resurrect a node deleted meanwhile.
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
        self._claims: dict[MirrorStableId, object] = {}
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
        observation = item.observation
        enriched = await self._try_enrich(observation, force_refresh=item.force_refresh)
        if enriched is None:
            self._release(observation.record.id, item.claim)
            return
        await self._put_claimed([(enriched, item.claim)])

    # --- Observation claims ---

    def _claim(self, node_id: MirrorStableId) -> object:
        claim = object()
        self._claims[node_id] = claim
        return claim

    def _release(self, node_id: MirrorStableId, claim: object) -> bool:
        """Consume *claim* if it is still the latest for *node_id*."""
        if self._claims.get(node_id) is not claim:
            return False
        del self._claims[node_id]
        return True

    async def _put_claimed(self, claimed: list[tuple[NodeObservation, object]]) -> None:
        """Enqueue a write of the observations whose claims are still current when it runs."""

        async def write() -> None:
            current = [o for o, claim in claimed if self._release(o.record.id, claim)]
            if current:
                await self._storage.apply_observations(current)

        await self._write_queue.put(write)

    # --- Shared primitives (used by multiple sources) ---

    async def upsert(self, observation: NodeObservation) -> NodeRecord:
        """Async-enrich, then enqueue a claimed write.

        Returns the (possibly enriched) record so callers can use it in responses.
        Used by: API (create, update, upload), webhook (delayed per-file upsert).
        """
        node_id = observation.record.id
        claim = self._claim(node_id)
        enriched = await self._try_enrich(observation, force_refresh=False)
        if enriched is None:
            self._release(node_id, claim)
            return observation.record
        await self._put_claimed([(enriched, claim)])
        return enriched.record

    async def delete(self, node_id: MirrorStableId) -> None:
        """Enqueue delete_subtree_and_emit_changes, superseding pending writes.

        Used by: API (delete node), webhook (file_removed).
        """
        self._claims.pop(node_id, None)
        await self._write_queue.put(
            partial(self._storage.delete_subtree_and_emit_changes, node_id)
        )

    async def remove_listed_child(
        self, node_id: MirrorStableId, parent_id: MirrorStableId
    ) -> None:
        """Delete *node_id* only if it is still a child of *parent_id* when applied.

        A listing that no longer contains a node does not prove deletion: the node
        may have moved after the mirror was read. Pending writes accepted before
        this call are superseded only when the removal applies.

        Used by: backfill.
        """
        claim = self._claims.get(node_id)

        async def remove() -> None:
            removed = await self._storage.delete_subtree_if_parent(node_id, parent_id)
            if removed and claim is not None:
                self._release(node_id, claim)

        await self._write_queue.put(remove)

    async def enrich(self, record: NodeRecord) -> NodeRecord:
        """Async media enrichment only — no persistence side-effects.

        Used by: webhook, which enriches before deciding the operation type.
        """
        return await self._enricher.enrich(record, force_refresh=False)

    # --- Batch primitives (shared by scan + webhook) ---

    async def upsert_batch(self, observations: list[NodeObservation]) -> None:
        """Async-enrich each observation, then enqueue a batch upsert.

        Uses PRESERVE_MEDIA_UPSERT_SQL so existing enriched dimensions are not
        overwritten by a re-scan or webhook update.

        Used by: scan (directory records per BFS level), webhook (batch upserts).
        """
        if not observations:
            return
        claims = [self._claim(o.record.id) for o in observations]
        enriched: list[tuple[NodeObservation, object]] = []
        for o, claim in zip(observations, claims, strict=True):
            if (e := await self._try_enrich(o, force_refresh=False)) is not None:
                enriched.append((e, claim))
            else:
                self._release(o.record.id, claim)
        if not enriched:
            return
        await self._put_claimed(enriched)

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

    async def upsert_file_batch(self, observations: list[NodeObservation]) -> None:
        """After directory rows are committed, enqueue per-file media enrichment.

        Waits for prior write-queue work so ``get_ancestors`` in the metadata worker
        sees parent rows from the preceding ``upsert_batch`` for this BFS level.
        When ``local_paths`` is unset, applies files directly without probing.

        Records whose existing DB row already has complete media dimensions are
        written directly, skipping the expensive metadata queue.
        """
        if not observations:
            return
        mq = self._metadata_queue
        await self._write_queue.join()

        node_ids = [o.record.id for o in observations]
        existing = await self._storage.get_nodes_by_ids(node_ids)

        skip_batch: list[tuple[NodeObservation, object]] = []
        for o in observations:
            claim = self._claim(o.record.id)
            db_rec = existing.get(o.record.id)
            if db_rec is not None and _has_complete_media_dims(db_rec):
                skip_batch.append((o, claim))
            else:
                await mq.put(
                    MetadataWorkItem(observation=o, force_refresh=True, claim=claim)
                )

        if skip_batch:
            await self._put_claimed(skip_batch)

    async def sync_nodes(self, records: list[NodeRecord]) -> None:
        """Enqueue bulk_upsert_nodes — upserts without emitting change rows.

        Used by: scanner on resume, to keep structural nodes (root, mounts) in sync
        with config without duplicating change-feed entries from the first run.
        """
        if not records:
            return
        await self._write_queue.put(partial(self._storage.bulk_upsert_nodes, records))

    # --- Reconcile / backfill ---

    async def reconcile_insert(self, from_api: NodeObservation) -> None:
        claim = self._claim(from_api.record.id)
        if from_api.record.is_directory:
            # Write in listing order so children never arrive before their parent.
            await self._put_claimed([(from_api, claim)])
            return
        await self._metadata_queue.put(
            MetadataWorkItem(observation=from_api, force_refresh=True, claim=claim)
        )

    async def reconcile_update(
        self, from_api: NodeObservation, existing: NodeRecord
    ) -> None:
        claim = self._claim(from_api.record.id)
        if _has_complete_media_dims(existing):
            await self._put_claimed([(from_api, claim)])
            return
        await self._metadata_queue.put(
            MetadataWorkItem(observation=from_api, force_refresh=True, claim=claim)
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

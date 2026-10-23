"""Tests for NodeSyncService methods and _has_complete_media_dims."""

import asyncio
import logging
from functools import partial
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.enricher import MediaEnrichmentError
from wcpan.drive.server.services.sync import (
    NodeSyncService,
    _has_complete_media_dims,
)
from wcpan.drive.server.types import NodeObservation


_EPOCH = 0

logging.getLogger("wcpan.drive.server.services.sync").setLevel(logging.CRITICAL + 1)


def _make_record(
    node_id: str = "n1",
    *,
    is_image: bool = False,
    is_video: bool = False,
    width: int = 0,
    height: int = 0,
    ms_duration: int = 0,
    is_directory: bool = False,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id="p1",
        name="test.txt",
        is_directory=is_directory,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type="text/plain",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=is_image,
        is_video=is_video,
        width=width,
        height=height,
        ms_duration=ms_duration,
    )


# ---------------------------------------------------------------------------
# _has_complete_media_dims
# ---------------------------------------------------------------------------


class TestHasCompleteMediaDims(IsolatedAsyncioTestCase):
    def test_non_media_returns_true(self):
        self.assertTrue(_has_complete_media_dims(_make_record()))

    def test_image_with_dimensions_returns_true(self):
        r = _make_record(is_image=True, width=100, height=200)
        self.assertTrue(_has_complete_media_dims(r))

    def test_image_without_dimensions_returns_false(self):
        r = _make_record(is_image=True, width=0, height=0)
        self.assertFalse(_has_complete_media_dims(r))

    def test_video_with_all_returns_true(self):
        r = _make_record(is_video=True, width=100, height=200, ms_duration=5000)
        self.assertTrue(_has_complete_media_dims(r))

    def test_video_without_duration_returns_false(self):
        r = _make_record(is_video=True, width=100, height=200, ms_duration=0)
        self.assertFalse(_has_complete_media_dims(r))

    def test_video_without_width_returns_false(self):
        r = _make_record(is_video=True, width=0, height=200, ms_duration=5000)
        self.assertFalse(_has_complete_media_dims(r))

    def test_directory_returns_true(self):
        r = _make_record(is_directory=True)
        self.assertTrue(_has_complete_media_dims(r))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeOffMain:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeStorage:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._bulk: list[NodeRecord] = []
        self._mount_states: list = []
        self._deferred: list = []
        self._observations: list[list[NodeRecord]] = []

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def get_nodes_by_ids(self, ids: list[str]) -> dict[str, NodeRecord]:
        return {nid: self._nodes[nid] for nid in ids if nid in self._nodes}

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> int:
        records = [o.record for o in observations]
        self._observations.append(records)
        for record in records:
            self._nodes[record.id] = record
        return len(records)

    async def delete_subtree_and_emit_changes(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    async def bulk_upsert_nodes(self, records: list[NodeRecord]) -> None:
        self._bulk.extend(records)

    async def set_mount_state(self, name: str, path: str, checkpoint: int) -> None:
        self._mount_states.append((name, path, checkpoint))

    async def apply_deferred_scan_removals(
        self, preserved: set[str], mount_ids: set[str]
    ) -> None:
        self._deferred.append((preserved, mount_ids))

    async def get_ancestors(self, node_id: str) -> list[NodeRecord]:
        return []


def _obs(record: NodeRecord) -> NodeObservation:
    return NodeObservation(record=record, locator=None)


def _make_service(
    storage: _FakeStorage | None = None,
    *,
    local_paths: dict[str, str] | None = None,
    metadata_queue: asyncio.Queue | None = None,
) -> tuple[NodeSyncService, _FakeStorage, asyncio.Queue]:
    storage = storage or _FakeStorage()
    wq: asyncio.Queue = asyncio.Queue()
    off_main = _FakeOffMain()
    cs = NodeSyncService(
        storage=storage,  # type: ignore[arg-type]
        write_queue=wq,
        off_main=off_main,  # type: ignore[arg-type]
        mounts={},
        local_paths=local_paths or {},
        metadata_queue=metadata_queue or asyncio.Queue(),
    )
    return cs, storage, wq


# ---------------------------------------------------------------------------
# NodeSyncService
# ---------------------------------------------------------------------------


class TestNodeSyncServiceInit(IsolatedAsyncioTestCase):
    def test_local_paths_property(self):
        cs, _, _ = _make_service()
        self.assertEqual(cs.local_paths, {})

        mq: asyncio.Queue = asyncio.Queue()
        cs2, _, _ = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        self.assertEqual(cs2.local_paths, {"/a": "/b"})


class TestNodeSyncServiceUpsert(IsolatedAsyncioTestCase):
    async def test_enqueues_write(self):
        cs, storage, wq = _make_service()
        record = _make_record()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda r, **kw: r,
        ):
            result = await cs.upsert(_obs(record))
        self.assertIs(result, record)
        self.assertEqual(wq.qsize(), 1)
        # Execute the enqueued write
        op = wq.get_nowait()
        await op()
        self.assertIn("n1", storage._nodes)

    async def test_enrichment_failure_skips_write(self):
        cs, _, wq = _make_service()
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            result = await cs.upsert(_obs(record))
        self.assertIs(result, record)
        self.assertEqual(wq.qsize(), 0)


class TestNodeSyncServiceDelete(IsolatedAsyncioTestCase):
    async def test_enqueues_delete(self):
        cs, storage, wq = _make_service()
        storage._nodes["n1"] = _make_record()
        await cs.delete("n1")
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertNotIn("n1", storage._nodes)


class TestNodeSyncServiceUpsertBatch(IsolatedAsyncioTestCase):
    async def test_empty_is_noop(self):
        cs, storage, wq = _make_service()
        await cs.upsert_batch([])
        self.assertEqual(wq.qsize(), 0)

    async def test_enqueues_batch(self):
        cs, storage, wq = _make_service()
        records = [_make_record("a"), _make_record("b")]
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda r, **kw: r,
        ):
            await cs.upsert_batch([_obs(r) for r in records])
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertEqual(len(storage._observations), 1)
        self.assertEqual(len(storage._observations[0]), 2)

    async def test_enrichment_failure_filters_batch_record(self):
        cs, storage, wq = _make_service()
        failed = _make_record("a", is_image=True)
        kept = _make_record("b")

        async def enrich(record, **_kw):
            if record is failed:
                raise MediaEnrichmentError(record, Path("/tmp/bad.jpg"))
            return record

        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=enrich,
        ):
            await cs.upsert_batch([_obs(failed), _obs(kept)])
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertEqual(storage._observations[0], [kept])

    async def test_enrichment_failure_skips_entire_batch_when_all_fail(self):
        cs, _, wq = _make_service()
        record = _make_record("a", is_image=True)
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            await cs.upsert_batch([_obs(record)])
        self.assertEqual(wq.qsize(), 0)


class TestNodeSyncServiceUpsertFileBatch(IsolatedAsyncioTestCase):
    async def test_empty_is_noop(self):
        cs, _, wq = _make_service()
        await cs.upsert_file_batch([])
        self.assertEqual(wq.qsize(), 0)

    async def test_with_local_paths_routes_to_metadata_queue(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, storage, wq = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        records = [_make_record("new-file", is_image=True)]
        await cs.upsert_file_batch([_obs(r) for r in records])
        # No existing DB record → goes to metadata queue
        self.assertEqual(mq.qsize(), 1)
        item = mq.get_nowait()
        self.assertEqual(item.observation.record.id, "new-file")
        self.assertTrue(item.force_refresh)

    async def test_with_local_paths_skips_complete_media(self):
        mq: asyncio.Queue = asyncio.Queue()
        storage = _FakeStorage()
        # Pre-populate DB with record that has complete dims
        storage._nodes["img1"] = _make_record(
            "img1", is_image=True, width=100, height=200
        )
        cs, storage, wq = _make_service(
            storage, local_paths={"/a": "/b"}, metadata_queue=mq
        )
        records = [_make_record("img1", is_image=True)]
        await cs.upsert_file_batch([_obs(r) for r in records])
        # Should skip metadata queue, enqueue write directly
        self.assertEqual(mq.qsize(), 0)
        self.assertGreaterEqual(wq.qsize(), 1)


class TestNodeSyncServiceSyncNodes(IsolatedAsyncioTestCase):
    async def test_empty_is_noop(self):
        cs, _, wq = _make_service()
        await cs.sync_nodes([])
        self.assertEqual(wq.qsize(), 0)

    async def test_enqueues_bulk_upsert(self):
        cs, storage, wq = _make_service()
        records = [_make_record("a"), _make_record("b")]
        await cs.sync_nodes(records)
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertEqual(len(storage._bulk), 2)


class TestNodeSyncServiceReconcile(IsolatedAsyncioTestCase):
    async def test_reconcile_insert(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, _ = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        record = _make_record("new")
        await cs.reconcile_insert(_obs(record))
        self.assertEqual(mq.qsize(), 1)

    async def test_reconcile_update_without_local_paths(self):
        cs, storage, wq = _make_service()
        existing = _make_record("n1")
        from_api = _make_record("n1")
        await cs.reconcile_update(_obs(from_api), existing)
        self.assertEqual(wq.qsize(), 1)

    async def test_reconcile_update_with_complete_dims(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, storage, wq = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        existing = _make_record("n1", is_image=True, width=100, height=200)
        from_api = _make_record("n1", is_image=True)
        await cs.reconcile_update(_obs(from_api), existing)
        # Complete dims → write queue, not metadata queue
        self.assertEqual(wq.qsize(), 1)
        self.assertEqual(mq.qsize(), 0)

    async def test_reconcile_update_without_complete_dims(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, storage, wq = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        existing = _make_record("n1", is_image=True, width=0, height=0)
        from_api = _make_record("n1", is_image=True)
        await cs.reconcile_update(_obs(from_api), existing)
        # Incomplete dims → metadata queue
        self.assertEqual(mq.qsize(), 1)
        self.assertEqual(wq.qsize(), 0)


class TestNodeSyncServiceLifecycle(IsolatedAsyncioTestCase):
    async def test_set_mount_watermark(self):
        from wcpan.synology import SynologyPath

        cs, storage, wq = _make_service()
        await cs.set_mount_watermark("docs", SynologyPath("/team-folders/docs"), 42)
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertEqual(storage._mount_states, [("docs", "/team-folders/docs", 42)])

    async def test_apply_deferred_removals(self):
        cs, storage, wq = _make_service()
        await cs.apply_deferred_removals({"a", "b"}, {"_docs"})
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertEqual(len(storage._deferred), 1)

    async def test_wait_enrichment_drained_noop_without_metadata(self):
        cs, _, _ = _make_service()
        await cs.wait_enrichment_drained()  # should not raise

    async def test_wait_enrichment_drained_waits(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(local_paths={"/a": "/b"}, metadata_queue=mq)
        # Both queues are empty, so join completes immediately
        await cs.wait_enrichment_drained()

    async def test_enrich_delegates_to_enricher(self):
        cs, _, _ = _make_service()
        record = _make_record()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            return_value=record,
        ) as mock_enrich:
            result = await cs.enrich(record)
        self.assertIs(result, record)
        mock_enrich.assert_awaited_once_with(record, force_refresh=False)

    async def test_process_metadata_item(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, storage, wq = _make_service(metadata_queue=mq)
        record = _make_record()
        await cs.reconcile_insert(_obs(record))
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            return_value=record,
        ):
            await cs.process_metadata_item(item)
        self.assertEqual(wq.qsize(), 1)
        op = wq.get_nowait()
        await op()
        self.assertIn("n1", storage._nodes)

    async def test_process_metadata_item_skips_enrichment_failure(self):
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(metadata_queue=mq)
        record = _make_record(is_image=True)
        await cs.reconcile_insert(_obs(record))
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            await cs.process_metadata_item(item)
        self.assertEqual(wq.qsize(), 0)

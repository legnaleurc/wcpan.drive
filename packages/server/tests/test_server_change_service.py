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
        # when / then
        self.assertTrue(_has_complete_media_dims(_make_record()))

    def test_image_with_dimensions_returns_true(self):
        # given
        r = _make_record(is_image=True, width=100, height=200)
        # when / then
        self.assertTrue(_has_complete_media_dims(r))

    def test_image_without_dimensions_returns_false(self):
        # given
        r = _make_record(is_image=True, width=0, height=0)
        # when / then
        self.assertFalse(_has_complete_media_dims(r))

    def test_video_with_all_returns_true(self):
        # given
        r = _make_record(is_video=True, width=100, height=200, ms_duration=5000)
        # when / then
        self.assertTrue(_has_complete_media_dims(r))

    def test_video_without_duration_returns_false(self):
        # given
        r = _make_record(is_video=True, width=100, height=200, ms_duration=0)
        # when / then
        self.assertFalse(_has_complete_media_dims(r))

    def test_video_without_width_returns_false(self):
        # given
        r = _make_record(is_video=True, width=0, height=200, ms_duration=5000)
        # when / then
        self.assertFalse(_has_complete_media_dims(r))

    def test_directory_returns_true(self):
        # given
        r = _make_record(is_directory=True)
        # when / then
        self.assertTrue(_has_complete_media_dims(r))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeOffMain:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeMirror:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._bulk: list[NodeRecord] = []
        self._observations: list[list[NodeRecord]] = []
        self._pending: set[str] = set()
        self._pending_backends: set[str] = set()

    async def mark_enrichment_pending(self, node_id: str, backend: str) -> None:
        self._pending.add(node_id)
        self._pending_backends.add(backend)

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def get_nodes_by_ids(self, ids: list[str]) -> dict[str, NodeRecord]:
        return {nid: self._nodes[nid] for nid in ids if nid in self._nodes}

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> set[str]:
        records = [o.record for o in observations]
        self._observations.append(records)
        for record in records:
            self._nodes[record.id] = record
        return {r.id for r in records}

    async def delete_subtree_and_emit_changes(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    async def bulk_upsert_nodes(self, records: list[NodeRecord]) -> None:
        self._bulk.extend(records)

    async def get_ancestors(self, node_id: str) -> list[NodeRecord]:
        return []


def _obs(record: NodeRecord) -> NodeObservation:
    return NodeObservation(record=record, locator=None)


def _make_service(
    mirror: _FakeMirror | None = None,
    *,
    metadata_queue: asyncio.Queue | None = None,
) -> tuple[NodeSyncService, _FakeMirror, asyncio.Queue]:
    cs, mirror, wq = _make_core_service(mirror, metadata_queue=metadata_queue)
    return cs.for_backend("b"), mirror, wq


def _make_core_service(
    mirror: _FakeMirror | None = None,
    *,
    metadata_queue: asyncio.Queue | None = None,
) -> tuple[NodeSyncService, _FakeMirror, asyncio.Queue]:
    """The unscoped service, as the core uses it for its own nodes."""
    mirror = mirror or _FakeMirror()
    wq: asyncio.Queue = asyncio.Queue()
    off_main = _FakeOffMain()
    cs = NodeSyncService(
        mirror=mirror,  # type: ignore[arg-type]
        write_queue=wq,
        off_main=off_main,  # type: ignore[arg-type]
        mounts={},
        metadata_queue=metadata_queue or asyncio.Queue(),
    )
    return cs, mirror, wq


# ---------------------------------------------------------------------------
# NodeSyncService
# ---------------------------------------------------------------------------


class TestNodeSyncServiceInit(IsolatedAsyncioTestCase):
    async def test_enqueues_write(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda r, **kw: r,
        ):
            # when
            async with cs.observe() as read:
                result = await cs.upsert(_obs(record), read=read)
        # then
        self.assertIs(result, record)
        self.assertEqual(wq.qsize(), 1)
        # when
        # Execute the enqueued write
        op = wq.get_nowait()
        await op()
        # then
        self.assertIn("n1", mirror._nodes)

    async def test_enrichment_failure_withholds_write_and_marks_pending(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            # when
            async with cs.observe() as read:
                result = await cs.upsert(_obs(record), read=read)
        # then
        self.assertIs(result, record)
        self.assertEqual(wq.qsize(), 1)
        # when
        await wq.get_nowait()()
        # then
        self.assertEqual(mirror._observations, [])
        self.assertEqual(mirror._pending, {record.id})
        self.assertEqual(mirror._pending_backends, {"b"})

    async def test_untagged_enrichment_failure_raises(self):
        # given
        cs, mirror, wq = _make_core_service()
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            # when / then
            async with cs.observe() as read:
                with self.assertRaises(ValueError):
                    await cs.upsert(_obs(record), read=read)
        self.assertEqual(wq.qsize(), 0)
        self.assertEqual(mirror._pending, set())

    async def test_untagged_retry_raises(self):
        # given
        cs, _, _ = _make_core_service()
        locate = AsyncMock()
        # when / then
        with self.assertRaises(ValueError):
            await cs.retry_pending_enrichment(locate=locate)
        locate.assert_not_awaited()


class TestNodeSyncServiceDelete(IsolatedAsyncioTestCase):
    async def test_enqueues_delete(self):
        # given
        cs, mirror, wq = _make_service()
        mirror._nodes["n1"] = _make_record()
        # when
        async with cs.observe() as read:
            await cs.delete("n1", read=read)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        op = wq.get_nowait()
        await op()
        # then
        self.assertNotIn("n1", mirror._nodes)


class TestNodeSyncServiceUpsertBatch(IsolatedAsyncioTestCase):
    async def test_empty_is_noop(self):
        # given
        cs, mirror, wq = _make_service()
        # when
        async with cs.observe() as read:
            await cs.upsert_batch([], read=read)
        # then
        self.assertEqual(wq.qsize(), 0)

    async def test_enqueues_batch(self):
        # given
        cs, mirror, wq = _make_service()
        records = [_make_record("a"), _make_record("b")]
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda r, **kw: r,
        ):
            # when
            async with cs.observe() as read:
                await cs.upsert_batch([_obs(r) for r in records], read=read)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        op = wq.get_nowait()
        await op()
        # then
        self.assertEqual(len(mirror._observations), 1)
        self.assertEqual(len(mirror._observations[0]), 2)

    async def test_enrichment_failure_filters_batch_record(self):
        # given
        cs, mirror, wq = _make_service()
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
            # when
            async with cs.observe() as read:
                await cs.upsert_batch([_obs(failed), _obs(kept)], read=read)
        # then
        self.assertEqual(wq.qsize(), 2)
        # when
        while not wq.empty():
            await wq.get_nowait()()
        # then
        self.assertEqual(mirror._observations, [[kept]])
        self.assertEqual(mirror._pending, {failed.id})

    async def test_enrichment_failure_skips_entire_batch_when_all_fail(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record("a", is_image=True)
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            # when
            async with cs.observe() as read:
                await cs.upsert_batch([_obs(record)], read=read)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        await wq.get_nowait()()
        # then
        self.assertEqual(mirror._observations, [])
        self.assertEqual(mirror._pending, {record.id})


class TestNodeSyncServiceUpsertFileBatch(IsolatedAsyncioTestCase):
    async def test_empty_is_noop(self):
        # given
        cs, _, wq = _make_service()
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([], read=read)
        # then
        self.assertEqual(wq.qsize(), 0)

    async def test_incomplete_media_routes_to_metadata_queue(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        records = [_make_record("new-file", is_image=True)]
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(r) for r in records], read=read)
        # then
        # No existing DB record → goes to metadata queue
        self.assertEqual(mq.qsize(), 1)
        item = mq.get_nowait()
        self.assertEqual(item.observation.record.id, "new-file")
        self.assertTrue(item.force_refresh)

    async def test_complete_media_skips_metadata_queue(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        mirror = _FakeMirror()
        # Pre-populate DB with record that has complete dims
        mirror._nodes["img1"] = _make_record(
            "img1", is_image=True, width=100, height=200
        )
        cs, mirror, wq = _make_service(mirror, metadata_queue=mq)
        records = [_make_record("img1", is_image=True)]
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(r) for r in records], read=read)
        # then
        # Should skip metadata queue, enqueue write directly
        self.assertEqual(mq.qsize(), 0)
        self.assertGreaterEqual(wq.qsize(), 1)


class TestNodeSyncServiceReconcile(IsolatedAsyncioTestCase):
    async def test_reconcile_insert(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, _ = _make_service(metadata_queue=mq)
        record = _make_record("new")
        # when
        async with cs.observe() as read:
            await cs.reconcile_insert(_obs(record), read=read)
        # then
        self.assertEqual(mq.qsize(), 1)

    async def test_reconcile_update_without_local_paths(self):
        # given
        cs, mirror, wq = _make_service()
        existing = _make_record("n1")
        from_api = _make_record("n1")
        # when
        async with cs.observe() as read:
            await cs.reconcile_update(_obs(from_api), existing, read=read)
        # then
        self.assertEqual(wq.qsize(), 1)

    async def test_reconcile_update_with_complete_dims(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        existing = _make_record("n1", is_image=True, width=100, height=200)
        from_api = _make_record("n1", is_image=True)
        # when
        async with cs.observe() as read:
            await cs.reconcile_update(_obs(from_api), existing, read=read)
        # then
        # Complete dims → write queue, not metadata queue
        self.assertEqual(wq.qsize(), 1)
        self.assertEqual(mq.qsize(), 0)

    async def test_reconcile_update_without_complete_dims(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        existing = _make_record("n1", is_image=True, width=0, height=0)
        from_api = _make_record("n1", is_image=True)
        # when
        async with cs.observe() as read:
            await cs.reconcile_update(_obs(from_api), existing, read=read)
        # then
        # Incomplete dims → metadata queue
        self.assertEqual(mq.qsize(), 1)
        self.assertEqual(wq.qsize(), 0)


class TestNodeSyncServiceLifecycle(IsolatedAsyncioTestCase):
    async def test_enqueue_runs_backend_write_in_queue_order(self):
        # given
        cs, _, wq = _make_service()
        job = AsyncMock()
        # when
        await cs.enqueue(job)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        await wq.get_nowait()()
        # then
        job.assert_awaited_once_with()

    async def test_wait_enrichment_drained_noop_without_metadata(self):
        # given
        cs, _, _ = _make_service()
        # when / then
        await cs.wait_enrichment_drained()  # should not raise

    async def test_wait_enrichment_drained_waits(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(metadata_queue=mq)
        # when / then
        # Both queues are empty, so join completes immediately
        await cs.wait_enrichment_drained()

    async def test_enrich_delegates_to_enricher(self):
        # given
        cs, _, _ = _make_service()
        record = _make_record()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            return_value=record,
        ) as mock_enrich:
            # when
            result = await cs.enrich(record)
        # then
        self.assertIs(result, record)
        mock_enrich.assert_awaited_once_with(record, force_refresh=False)

    async def test_process_metadata_item(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        record = _make_record()
        async with cs.observe() as read:
            await cs.reconcile_insert(_obs(record), read=read)
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            return_value=record,
        ):
            # when
            await cs.process_metadata_item(item)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        op = wq.get_nowait()
        await op()
        # then
        self.assertIn("n1", mirror._nodes)

    async def test_process_metadata_item_withholds_enrichment_failure(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        record = _make_record(is_image=True)
        async with cs.observe() as read:
            await cs.reconcile_insert(_obs(record), read=read)
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server.services.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, Path("/tmp/bad.jpg")),
        ):
            # when
            await cs.process_metadata_item(item)
        # then
        self.assertEqual(wq.qsize(), 1)
        # when
        await wq.get_nowait()()
        # then
        self.assertEqual(mirror._observations, [])
        self.assertEqual(mirror._pending, {record.id})

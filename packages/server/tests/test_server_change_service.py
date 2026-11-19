"""Tests for NodeSyncService methods and _has_complete_media_dims."""

import asyncio
import contextlib
import logging
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server._lib.write_queue import (
    write_worker,
)
from wcpan.drive.server._services.media.enricher import MediaEnrichmentError
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import (
    ProbeFailure,
    ProbeUnavailableError,
)
from wcpan.drive.server._services.media.probing import (
    MediaProbing,
    ProbesNotDeclaredError,
)
from wcpan.drive.server._services.mirror.model import (
    MediaFailure,
)
from wcpan.drive.server._services.sync.metadata import (
    METADATA_WORKER_COUNT,
)
from wcpan.drive.server._services.sync.service import (
    NodeSyncService,
    WriterStoppedError,
    _has_complete_media_dims,
)
from wcpan.drive.server._types import NodeObservation


_EPOCH = 0

logging.getLogger("wcpan.drive.server._services.sync.service").setLevel(
    logging.CRITICAL + 1
)


def _make_record(
    node_id: str = "n1",
    *,
    is_image: bool = False,
    is_video: bool = False,
    width: int = 0,
    height: int = 0,
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

    def test_video_with_dimensions_returns_true(self):
        # given
        r = _make_record(is_video=True, width=100, height=200)
        # when / then
        self.assertTrue(_has_complete_media_dims(r))

    def test_video_without_width_returns_false(self):
        # given
        r = _make_record(is_video=True, width=0, height=200)
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

    async def mark_media_failure(
        self, backend: str, source_key: str, kind: ProbeFailure
    ) -> None:
        self._pending.add(source_key)
        self._pending_backends.add(backend)

    async def clear_media_failure(self, backend: str, source_key: str) -> None:
        self._pending.discard(source_key)

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
    # The source key is the ID, so failure records name the node.
    return NodeObservation(record=record, locator=None, source_key=record.id)


def _make_service(
    mirror: _FakeMirror | None = None,
    *,
    metadata_queue: asyncio.Queue | None = None,
    write_queue: asyncio.Queue | None = None,
) -> tuple[NodeSyncService, _FakeMirror, asyncio.Queue]:
    cs, mirror, wq = _make_core_service(
        mirror, metadata_queue=metadata_queue, write_queue=write_queue
    )
    return cs.for_backend("b"), mirror, wq


def _without_writer(cs: NodeSyncService) -> NodeSyncService:
    """*cs* for a write queue no worker consumes.

    These tests run queued jobs by hand, so a barrier job would never land;
    the barrier is tested where a write worker runs.
    """
    setattr(cs, "barrier", AsyncMock())
    return cs


def _make_core_service(
    mirror: _FakeMirror | None = None,
    *,
    metadata_queue: asyncio.Queue | None = None,
    write_queue: asyncio.Queue | None = None,
) -> tuple[NodeSyncService, _FakeMirror, asyncio.Queue]:
    """The unscoped service, as the core uses it for its own nodes."""
    mirror = mirror or _FakeMirror()
    wq: asyncio.Queue = write_queue if write_queue is not None else asyncio.Queue()
    off_main = _FakeOffMain()
    cs = NodeSyncService(
        mirror=mirror,  # type: ignore[arg-type]
        write_queue=wq,
        probing=MediaProbing(runner=ProbeRunner()),
        metadata_queue=metadata_queue or asyncio.Queue(),
    )
    return _without_writer(cs), mirror, wq


# ---------------------------------------------------------------------------
# NodeSyncService
# ---------------------------------------------------------------------------


class TestNodeSyncServiceInit(IsolatedAsyncioTestCase):
    async def test_enqueues_write(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record()
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda o, **kw: o.record,
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

    async def test_enrichment_failure_withholds_new_file_and_records_it(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(
                record, ProbeFailure.INVALID, "unreadable"
            ),
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
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(
                record, ProbeFailure.INVALID, "unreadable"
            ),
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
            await cs.retry_media_failures(locate=locate)
        locate.assert_not_awaited()


class TestMediaFailurePolicy(IsolatedAsyncioTestCase):
    """Only a file never probed successfully is withheld."""

    async def _upsert(self, cs, record, *, failing: bool = True, strict=False):
        effect = (
            MediaEnrichmentError(record, ProbeFailure.INVALID, "unreadable")
            if failing
            else (lambda r, **kw: r)
        )
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=effect,
        ) as enrich:
            async with cs.observe() as read:
                result = await cs.upsert(_obs(record), read=read, strict=strict)
        return result, enrich

    async def test_published_file_keeps_its_last_probe_when_probe_fails(self):
        # given: published with dimensions; its content changed
        cs, mirror, wq = _make_service()
        mirror._nodes["n1"] = _make_record(is_image=True, width=10, height=20)
        changed = replace(_make_record(is_image=True), size=5, name="new.jpg")
        # when
        await self._upsert(cs, changed)
        while not wq.empty():
            await wq.get_nowait()()
        # then: the new metadata is written with the stored dimensions
        self.assertEqual(
            mirror._observations, [[replace(changed, width=10, height=20)]]
        )
        self.assertEqual(mirror._pending, set())

    async def test_failed_probe_keeps_stored_dims_over_observed_ones(self):
        # given: the observation carries other, stale dimensions
        cs, mirror, wq = _make_service()
        mirror._nodes["n1"] = _make_record(is_image=True, width=10, height=20)
        changed = replace(
            _make_record(is_image=True, width=5, height=5), size=5, name="new.jpg"
        )
        # when
        await self._upsert(cs, changed)
        while not wq.empty():
            await wq.get_nowait()()
        # then
        self.assertEqual(
            mirror._observations, [[replace(changed, width=10, height=20)]]
        )

    async def test_unchanged_content_reuses_dimensions_without_probing(self):
        # given: a rename of a published image
        cs, mirror, wq = _make_service()
        mirror._nodes["n1"] = _make_record(is_image=True, width=10, height=20)
        renamed = replace(_make_record(is_image=True), name="renamed.jpg")
        # when
        result, enrich = await self._upsert(cs, renamed)
        # then
        enrich.assert_not_awaited()
        self.assertEqual((result.width, result.height), (10, 20))
        self.assertEqual(result.name, "renamed.jpg")

    async def test_rename_into_a_media_kind_is_probed(self):
        # given: a published text file renamed to an image
        cs, mirror, _ = _make_service()
        mirror._nodes["n1"] = _make_record()
        # when
        _, enrich = await self._upsert(cs, _make_record(is_image=True), failing=False)
        # then
        enrich.assert_awaited_once()

    async def test_changed_content_is_probed_again(self):
        # given
        cs, mirror, _ = _make_service()
        mirror._nodes["n1"] = _make_record(is_image=True, width=10, height=20)
        changed = replace(_make_record(is_image=True), hash="other")
        # when
        _, enrich = await self._upsert(cs, changed, failing=False)
        # then
        enrich.assert_awaited_once()

    async def test_strict_upsert_raises_and_writes_nothing(self):
        # given
        cs, mirror, wq = _make_service()
        record = _make_record(is_image=True)
        # when / then
        with self.assertRaises(MediaEnrichmentError):
            await self._upsert(cs, record, strict=True)
        self.assertEqual(wq.qsize(), 0)
        self.assertEqual(mirror._pending, set())

    async def test_retry_drops_records_of_files_gone(self):
        # given
        cs, mirror, wq = _make_service()
        mirror._pending = {"gone"}
        mirror.get_media_failures = AsyncMock(
            return_value=[MediaFailure("gone", ProbeFailure.INVALID)]
        )  # type: ignore[method-assign]
        locate = AsyncMock(return_value=None)
        # when
        await cs.retry_media_failures(locate=locate)
        while not wq.empty():
            await wq.get_nowait()()
        # then
        locate.assert_awaited_once_with("gone")
        self.assertEqual(mirror._pending, set())


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
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=lambda o, **kw: o.record,
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

        async def enrich(observation, **_kw):
            record = observation.record
            if record is failed:
                raise MediaEnrichmentError(record, ProbeFailure.INVALID, "unreadable")
            return record

        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=enrich,
        ):
            # when
            async with cs.observe() as read:
                await cs.upsert_batch([_obs(failed), _obs(kept)], read=read)
        # then: one write decides the failure and publishes the rest
        self.assertEqual(wq.qsize(), 1)
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
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(
                record, ProbeFailure.INVALID, "unreadable"
            ),
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

    async def test_new_file_that_is_not_media_writes_directly(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(metadata_queue=mq)
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(_make_record("n1"))], read=read)
        # then
        self.assertEqual((mq.qsize(), wq.qsize()), (0, 1))

    async def test_changed_file_that_is_not_media_writes_directly(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        mirror = _FakeMirror()
        mirror._nodes["n1"] = _make_record("n1")
        cs, mirror, wq = _make_service(mirror, metadata_queue=mq)
        changed = replace(_make_record("n1"), size=99, hash="changed")
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(changed)], read=read)
        # then
        self.assertEqual((mq.qsize(), wq.qsize()), (0, 1))
        await wq.get_nowait()()
        self.assertEqual(mirror._nodes["n1"].size, 99)

    async def test_unchanged_other_file_writes_directly(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        mirror = _FakeMirror()
        mirror._nodes["n1"] = _make_record("n1")
        cs, mirror, wq = _make_service(mirror, metadata_queue=mq)
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(_make_record("n1"))], read=read)
        # then
        self.assertEqual((mq.qsize(), wq.qsize()), (0, 1))

    async def test_published_media_without_dims_routes_to_metadata_queue(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        mirror = _FakeMirror()
        mirror._nodes["n1"] = _make_record("n1", is_image=True, width=0, height=0)
        cs, mirror, wq = _make_service(mirror, metadata_queue=mq)
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch(
                [_obs(_make_record("n1", is_image=True))], read=read
            )
        # then
        self.assertEqual((mq.qsize(), wq.qsize()), (1, 0))
        self.assertTrue(mq.get_nowait().force_refresh)


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

    async def test_process_metadata_item(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, mirror, wq = _make_service(metadata_queue=mq)
        record = _make_record(is_image=True)
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(record)], read=read)
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
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
            await cs.upsert_file_batch([_obs(record)], read=read)
        item = mq.get_nowait()
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(
                record, ProbeFailure.INVALID, "unreadable"
            ),
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


def _held(cs: NodeSyncService) -> dict:
    """Reads still outstanding: in progress or held by a pending write."""
    return dict(cs._order._outstanding)


async def _blocked(coro) -> asyncio.Task:
    """Start *coro* and let it run until it blocks."""
    task = asyncio.create_task(coro)
    for _ in range(5):
        await asyncio.sleep(0)
    return task


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestHeldReadOwnership(IsolatedAsyncioTestCase):
    """A held read is released exactly once, whatever stops its handoff."""

    async def test_process_metadata_item_cancelled_in_probe_releases(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(metadata_queue=mq)
        record = _make_record(is_image=True)
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(record)], read=read)
        item = mq.get_nowait()
        probing = asyncio.Event()

        async def hang(*args, **kwargs):
            probing.set()
            await asyncio.Event().wait()

        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new=hang,
        ):
            task = asyncio.create_task(cs.process_metadata_item(item))
            await probing.wait()
            # when
            await _cancel(task)
        # then
        self.assertEqual(_held(cs), {})
        self.assertEqual(wq.qsize(), 0)

    async def test_process_metadata_item_after_enqueue_job_releases(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs, _, wq = _make_service(metadata_queue=mq)
        record = _make_record(is_image=True)
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(record)], read=read)
        item = mq.get_nowait()
        # when
        await cs.process_metadata_item(item)
        # then: the queued write owns the read until it runs
        self.assertEqual(_held(cs), {read: 1})
        # when
        await wq.get_nowait()()
        # then
        self.assertEqual(_held(cs), {})

    async def test_media_failure_cancelled_on_full_write_queue_releases(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        wq: asyncio.Queue = asyncio.Queue(maxsize=1)
        cs, _, _ = _make_service(metadata_queue=mq, write_queue=wq)
        record = _make_record(is_image=True)
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(record)], read=read)
        item = mq.get_nowait()
        wq.put_nowait(AsyncMock())
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=MediaEnrichmentError(record, ProbeFailure.INVALID, "bad"),
        ):
            task = await _blocked(cs.process_metadata_item(item))
            # when
            await _cancel(task)
        # then: both the item's read and the failure record's hold are released
        self.assertEqual(_held(cs), {})

    async def test_upsert_batch_cancelled_in_probe_releases(self):
        # given
        cs, _, _ = _make_service()
        records = [_make_record(f"n{i}", is_image=True) for i in range(3)]

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new=hang,
        ):
            async with cs.observe() as read:
                task = await _blocked(
                    cs.upsert_batch([_obs(r) for r in records], read=read)
                )
                # when
                await _cancel(task)
        # then
        self.assertEqual(_held(cs), {})

    async def test_upsert_file_batch_cancelled_on_full_metadata_queue_releases(self):
        # given
        mq: asyncio.Queue = asyncio.Queue(maxsize=1)
        cs, _, _ = _make_service(metadata_queue=mq)
        records = [_make_record(f"n{i}", is_image=True) for i in range(3)]
        async with cs.observe() as read:
            task = await _blocked(
                cs.upsert_file_batch([_obs(r) for r in records], read=read)
            )
            # when: the first item is queued, the second waits for room
            await _cancel(task)
        # then: only the queued item still holds the read
        self.assertEqual(_held(cs), {read: 1})
        mq.get_nowait()


class TestEnrichmentScope(IsolatedAsyncioTestCase):
    async def test_worker_failure_propagates(self):
        # given
        cs, _, wq = _make_service()
        writer = await self._run_writes(wq)
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            side_effect=ProbeUnavailableError("no mediainfo"),
        ):
            # when / then
            with self.assertRaises(BaseExceptionGroup) as raised:
                async with cs.enrichment_scope():
                    async with cs.observe() as read:
                        await cs.upsert_file_batch([_obs(record)], read=read)
        self.assertTrue(raised.exception.subgroup(ProbeUnavailableError))
        self.assertEqual(_held(cs), {})
        await _cancel(writer)

    async def _run_writes(self, wq: asyncio.Queue) -> asyncio.Task:
        async def worker():
            while True:
                job = await wq.get()
                try:
                    await job()
                finally:
                    wq.task_done()

        return asyncio.create_task(worker())

    async def test_cancellation_does_not_wait_for_queued_writes(self):
        # given: a write queued with no writer running to land it
        cs, mirror, wq = _make_service()
        entered = asyncio.Event()
        record = _make_record("d", is_directory=True)

        async def command() -> None:
            async with cs.enrichment_scope():
                async with cs.observe() as read:
                    await cs.upsert_file_batch([_obs(record)], read=read)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(command())
        await entered.wait()
        # when
        task.cancel()
        # then: the write is abandoned, for the next run to observe again
        async with asyncio.timeout(1):
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotIn(record.id, mirror._nodes)
        self.assertEqual(wq.qsize(), 1)

    async def test_cancellation_after_the_write_worker_failed_does_not_hang(self):
        # given: the writer failed with another write still queued
        wq: asyncio.Queue = asyncio.Queue()
        failing = AsyncMock(side_effect=RuntimeError("disk full"))
        wq.put_nowait(failing)
        wq.put_nowait(AsyncMock())
        writer = asyncio.create_task(write_worker(wq))
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            await writer
        cs = NodeSyncService(
            mirror=_FakeMirror(),  # type: ignore[arg-type]
            write_queue=wq,
            metadata_queue=asyncio.Queue(),
            probing=MediaProbing(runner=None),
            writer=writer,
        ).for_backend("b")
        entered = asyncio.Event()

        async def command() -> None:
            async with cs.enrichment_scope():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(command())
        await entered.wait()
        # when
        task.cancel()
        # then
        async with asyncio.timeout(1):
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The queue still says a write never landed.
        self.assertEqual(wq.qsize(), 1)

    async def test_flush_after_the_only_write_failed_raises(self):
        # given: the writer failed on the last queued write
        wq: asyncio.Queue = asyncio.Queue()
        wq.put_nowait(AsyncMock(side_effect=RuntimeError("disk full")))
        writer = asyncio.create_task(write_worker(wq))
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            await writer
        cs = NodeSyncService(
            mirror=_FakeMirror(),  # type: ignore[arg-type]
            write_queue=wq,
            metadata_queue=asyncio.Queue(),
            probing=MediaProbing(runner=None),
            writer=writer,
        )
        # when / then: the failed write is not reported as landed
        async with asyncio.timeout(1):
            with self.assertRaises(WriterStoppedError):
                await cs.flush()

    async def test_success_drains_enrichment_and_writes(self):
        # given
        cs, mirror, wq = _make_service()
        writer = await self._run_writes(wq)
        record = _make_record(is_image=True)
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new_callable=AsyncMock,
            return_value=replace(record, width=1, height=1),
        ):
            # when
            async with cs.enrichment_scope():
                async with cs.observe() as read:
                    await cs.upsert_file_batch([_obs(record)], read=read)
        # then
        self.assertIn(record.id, mirror._nodes)
        self.assertEqual(_held(cs), {})
        await _cancel(writer)

    async def test_error_cancels_probes_and_reraises(self):
        # given
        cs, _, wq = _make_service()
        writer = await self._run_writes(wq)
        probing = 0
        cancelled = 0
        started = asyncio.Event()

        async def hang(*args, **kwargs):
            nonlocal probing, cancelled
            probing += 1
            if probing >= METADATA_WORKER_COUNT:
                started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        files = [
            _make_record(f"n{i}", is_image=True)
            for i in range(METADATA_WORKER_COUNT + 1)
        ]
        with patch(
            "wcpan.drive.server._services.media.enricher.MediaEnrichService.enrich",
            new=hang,
        ):
            # when
            with self.assertRaisesRegex(RuntimeError, "traversal failed"):
                async with cs.enrichment_scope():
                    async with cs.observe() as read:
                        for f in files:
                            await cs.upsert_file_batch([_obs(f)], read=read)
                        await started.wait()
                        raise RuntimeError("traversal failed")
        # then
        self.assertEqual(cancelled, METADATA_WORKER_COUNT)
        await _cancel(writer)


class TestUndeclaredProbing(IsolatedAsyncioTestCase):
    """Without a runner, both probe paths stop and leave no read held."""

    def _service(self, mq: asyncio.Queue | None = None) -> NodeSyncService:
        cs = NodeSyncService(
            mirror=_FakeMirror(),  # type: ignore[arg-type]
            write_queue=asyncio.Queue(),
            metadata_queue=mq or asyncio.Queue(),
            probing=MediaProbing(runner=None),
        )
        return _without_writer(cs).for_backend("b")

    async def test_inline_upsert_raises(self):
        # given
        cs = self._service()
        # when / then
        async with cs.observe() as read:
            with self.assertRaises(ProbesNotDeclaredError):
                await cs.upsert(_obs(_make_record(is_image=True)), read=read)
        self.assertEqual(_held(cs), {})

    async def test_queued_item_raises(self):
        # given
        mq: asyncio.Queue = asyncio.Queue()
        cs = self._service(mq)
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(_make_record(is_image=True))], read=read)
        # when / then
        with self.assertRaises(ProbesNotDeclaredError):
            await cs.process_metadata_item(mq.get_nowait())
        self.assertEqual(_held(cs), {})

    async def test_non_media_upsert_succeeds(self):
        # given
        cs = self._service()
        # when
        async with cs.observe() as read:
            record = await cs.upsert(_obs(_make_record()), read=read)
        # then
        self.assertEqual(record.id, "n1")


class TestOfflineWorkersRequired(IsolatedAsyncioTestCase):
    """Offline, queued enrichment needs ``enrichment_scope`` to run workers."""

    def _service(self) -> tuple[NodeSyncService, asyncio.Queue]:
        mq: asyncio.Queue = asyncio.Queue()
        cs = NodeSyncService(
            mirror=_FakeMirror(),  # type: ignore[arg-type]
            write_queue=asyncio.Queue(),
            metadata_queue=mq,
            probing=MediaProbing(runner=None),
            require_workers=True,
        )
        return _without_writer(cs).for_backend("b"), mq

    async def test_queuing_without_a_scope_raises(self):
        # given
        cs, mq = self._service()
        # when / then
        async with cs.observe() as read:
            with self.assertRaisesRegex(RuntimeError, "no metadata workers"):
                await cs.upsert_file_batch(
                    [_obs(_make_record(is_image=True))], read=read
                )
        self.assertEqual(mq.qsize(), 0)
        self.assertEqual(_held(cs), {})

    async def test_writes_that_need_no_enrichment_pass(self):
        # given
        cs, mq = self._service()
        # when
        async with cs.observe() as read:
            await cs.upsert_batch(
                [_obs(_make_record("d", is_directory=True))], read=read
            )
        # then
        self.assertEqual(mq.qsize(), 0)

    async def test_files_that_are_not_media_need_no_workers(self):
        # given
        cs, mq = self._service()
        # when
        async with cs.observe() as read:
            await cs.upsert_file_batch([_obs(_make_record("f"))], read=read)
        # then
        self.assertEqual(mq.qsize(), 0)


class TestBarrier(IsolatedAsyncioTestCase):
    def _service(
        self, wq: asyncio.Queue, writer: "asyncio.Task[None]"
    ) -> NodeSyncService:
        return NodeSyncService(
            mirror=_FakeMirror(),  # type: ignore[arg-type]
            write_queue=wq,
            metadata_queue=asyncio.Queue(),
            probing=MediaProbing(runner=None),
            writer=writer,
        )

    async def test_waits_for_writes_queued_before_it(self):
        # given: a write that lands only when released
        wq: asyncio.Queue = asyncio.Queue()
        release = asyncio.Event()
        landed: list[str] = []

        async def earlier() -> None:
            await release.wait()
            landed.append("earlier")

        wq.put_nowait(earlier)
        writer = asyncio.create_task(write_worker(wq))
        cs = self._service(wq, writer)
        # when
        barrier = asyncio.create_task(cs.barrier())
        await asyncio.sleep(0.01)
        # then: still waiting on the earlier write
        self.assertFalse(barrier.done())
        release.set()
        async with asyncio.timeout(1):
            await barrier
        self.assertEqual(landed, ["earlier"])
        await _cancel(writer)

    async def test_does_not_wait_for_writes_queued_after_it(self):
        # given: the writer is busy, so queued jobs stay visible
        wq: asyncio.Queue = asyncio.Queue()
        release = asyncio.Event()
        wq.put_nowait(release.wait)
        writer = asyncio.create_task(write_worker(wq))
        cs = self._service(wq, writer)
        async with asyncio.timeout(1):
            while wq.qsize() > 0:
                await asyncio.sleep(0)
        barrier = asyncio.create_task(cs.barrier())
        async with asyncio.timeout(1):
            while wq.qsize() < 1:
                await asyncio.sleep(0)
        # when: a write that never lands is queued after the barrier's job
        await wq.put(asyncio.Event().wait)
        release.set()
        # then
        async with asyncio.timeout(1):
            await barrier
        await _cancel(writer)

    async def test_raises_when_the_writer_stopped(self):
        # given: the writer failed on the write before the barrier
        wq: asyncio.Queue = asyncio.Queue()
        wq.put_nowait(AsyncMock(side_effect=RuntimeError("disk full")))
        writer = asyncio.create_task(write_worker(wq))
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            await writer
        cs = self._service(wq, writer)
        # when / then
        async with asyncio.timeout(1):
            with self.assertRaises(WriterStoppedError):
                await cs.barrier()

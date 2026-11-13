"""Tests for write queue helpers and worker coroutines."""

import asyncio
import logging
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.types import MetadataWorkItem, NodeObservation, ReadSeq
from wcpan.drive.server.workers import (
    create_write_queue,
    metadata_worker,
    write_worker,
)


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


_EPOCH = 0


def _make_record(node_id: str = "n1") -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id="p1",
        name="x",
        is_directory=False,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type="text/plain",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class TestWriteQueue(IsolatedAsyncioTestCase):
    async def test_puts_op_on_queue(self):
        # given
        q: asyncio.Queue = asyncio.Queue()
        seen: list[int] = []

        async def op() -> None:
            seen.append(42)

        # when
        await q.put(op)

        # then — op is on the queue but not yet executed
        self.assertEqual(q.qsize(), 1)
        job = q.get_nowait()
        await job()
        self.assertEqual(seen, [42])

    async def test_blocks_when_queue_full(self):
        # given — bounded queue at capacity
        q: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def noop() -> None:
            pass

        await q.put(noop)  # fill it

        enqueued = False

        async def producer() -> None:
            nonlocal enqueued
            await q.put(noop)
            enqueued = True

        # when
        prod = asyncio.create_task(producer())
        await asyncio.sleep(0)
        # then
        # queue still full — producer should be blocked
        self.assertFalse(enqueued)

        # when
        # drain one item to unblock
        q.get_nowait()
        q.task_done()
        await prod
        # then
        self.assertTrue(enqueued)


class TestMetadataWorker(IsolatedAsyncioTestCase):
    async def test_processes_item(self):
        # given
        q: asyncio.Queue[MetadataWorkItem] = asyncio.Queue()
        processed: list[str] = []

        async def process(item: MetadataWorkItem) -> None:
            processed.append(item.observation.record.id)

        item = MetadataWorkItem(
            observation=NodeObservation(record=_make_record("n1"), locator=None),
            force_refresh=False,
            read=ReadSeq(0),
        )
        await q.put(item)

        # when
        worker = asyncio.create_task(metadata_worker(q, process))
        await q.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # then
        self.assertEqual(processed, ["n1"])

    async def test_exception_does_not_stop_worker(self):
        # given
        q: asyncio.Queue[MetadataWorkItem] = asyncio.Queue()
        call_count = 0

        async def process(item: MetadataWorkItem) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("boom")

        await q.put(
            MetadataWorkItem(
                observation=NodeObservation(record=_make_record("a"), locator=None),
                force_refresh=False,
                read=ReadSeq(0),
            )
        )
        await q.put(
            MetadataWorkItem(
                observation=NodeObservation(record=_make_record("b"), locator=None),
                force_refresh=False,
                read=ReadSeq(0),
            )
        )

        # when
        worker = asyncio.create_task(metadata_worker(q, process))
        await q.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # then
        self.assertEqual(call_count, 2)


class TestWriteWorker(IsolatedAsyncioTestCase):
    async def test_processes_task(self):
        # given
        q: asyncio.Queue = asyncio.Queue()
        executed: list[int] = []

        async def op() -> None:
            executed.append(1)

        await q.put(op)

        # when
        worker = asyncio.create_task(write_worker(q))
        await q.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # then
        self.assertEqual(executed, [1])

    async def test_exception_propagates(self):
        # given
        q: asyncio.Queue = asyncio.Queue()

        async def boom() -> None:
            raise RuntimeError("db error")

        await q.put(boom)

        # when
        worker = asyncio.create_task(write_worker(q))
        # then
        with self.assertRaises(RuntimeError):
            await worker

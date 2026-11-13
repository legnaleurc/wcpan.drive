"""Tests for write queue helpers and worker coroutines."""

import asyncio
import logging
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.types import MetadataWorkItem, NodeObservation, ReadSeq
from wcpan.drive.server.workers import (
    create_checkpoint_scheduler,
    create_write_queue,
    metadata_worker,
    noop_after_write,
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
        worker = asyncio.create_task(write_worker(q, noop_after_write))
        await q.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # then
        self.assertEqual(executed, [1])

    async def test_successful_task_calls_after_write(self):
        # given
        q: asyncio.Queue = asyncio.Queue()
        after_write = MagicMock()

        async def op() -> None:
            pass

        await q.put(op)

        # when
        worker = asyncio.create_task(write_worker(q, after_write))
        await q.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # then
        after_write.assert_called_once_with()

    async def test_exception_propagates(self):
        # given
        q: asyncio.Queue = asyncio.Queue()

        async def boom() -> None:
            raise RuntimeError("db error")

        await q.put(boom)

        # when
        worker = asyncio.create_task(write_worker(q, noop_after_write))
        # then
        with self.assertRaises(RuntimeError):
            await worker

    async def test_failed_task_does_not_call_after_write(self):
        # given
        q: asyncio.Queue = asyncio.Queue()
        after_write = MagicMock()

        async def boom() -> None:
            raise RuntimeError("db error")

        await q.put(boom)

        # when
        worker = asyncio.create_task(write_worker(q, after_write))
        # then
        with self.assertRaises(RuntimeError):
            await worker
        after_write.assert_not_called()


async def _noop() -> None:
    pass


class TestCheckpointScheduler(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mirror = MagicMock()
        self.mirror.checkpoint = AsyncMock()
        self.queue = create_write_queue()

    async def test_checkpoint_is_queued_not_run_directly(self):
        # when: no worker consumes the queue
        async with asyncio.TaskGroup() as group:
            schedule = create_checkpoint_scheduler(
                group, self.mirror, self.queue, delay=0.01
            )
            schedule()
            await asyncio.sleep(0.05)

        # then
        self.assertEqual(self.queue.qsize(), 1)
        self.mirror.checkpoint.assert_not_called()

    async def test_full_queue_does_not_hold_shutdown(self):
        # given: a full queue whose worker has stopped
        while not self.queue.full():
            self.queue.put_nowait(_noop)

        # when: the timer fires before shutdown
        async with asyncio.timeout(1):
            async with asyncio.TaskGroup() as group:
                schedule = create_checkpoint_scheduler(
                    group, self.mirror, self.queue, delay=0
                )
                schedule()
                await asyncio.sleep(0.05)
                schedule.cancel()

        # then: the checkpoint is skipped; a later write schedules it again
        self.assertNotIn(
            schedule._checkpoint,  # pyright: ignore[reportPrivateUsage]
            list(self.queue._queue),  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownArgumentType]
        )
        self.mirror.checkpoint.assert_not_called()

    async def test_checkpoints_once_after_writes(self):
        # when
        async with asyncio.TaskGroup() as group:
            schedule = create_checkpoint_scheduler(
                group, self.mirror, self.queue, delay=0.01
            )
            worker = group.create_task(write_worker(self.queue, schedule))
            await self.queue.put(_noop)
            # long enough for a checkpoint to schedule another
            await asyncio.sleep(0.1)
            worker.cancel()

        # then
        self.mirror.checkpoint.assert_awaited_once()

    async def test_does_not_checkpoint_without_writes(self):
        # when
        async with asyncio.TaskGroup() as group:
            schedule = create_checkpoint_scheduler(
                group, self.mirror, self.queue, delay=0.01
            )
            worker = group.create_task(write_worker(self.queue, schedule))
            await asyncio.sleep(0.03)
            worker.cancel()

        # then
        self.mirror.checkpoint.assert_not_called()

    async def test_debounces_multiple_writes(self):
        # when
        async with asyncio.TaskGroup() as group:
            schedule = create_checkpoint_scheduler(
                group, self.mirror, self.queue, delay=0.03
            )
            worker = group.create_task(write_worker(self.queue, schedule))
            for _ in range(3):
                await self.queue.put(_noop)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            worker.cancel()

        # then
        self.mirror.checkpoint.assert_awaited_once()

    async def test_checkpoints_again_after_later_writes(self):
        # when
        async with asyncio.TaskGroup() as group:
            schedule = create_checkpoint_scheduler(
                group, self.mirror, self.queue, delay=0.01
            )
            worker = group.create_task(write_worker(self.queue, schedule))
            await self.queue.put(_noop)
            await asyncio.sleep(0.05)
            await self.queue.put(_noop)
            await asyncio.sleep(0.05)
            worker.cancel()

        # then
        self.assertEqual(self.mirror.checkpoint.await_count, 2)

    async def test_checkpoint_failure_stops_the_write_worker(self):
        # given
        self.mirror.checkpoint = AsyncMock(
            side_effect=RuntimeError("checkpoint failed")
        )

        # when
        with self.assertRaises(ExceptionGroup) as cm:
            async with asyncio.TaskGroup() as group:
                schedule = create_checkpoint_scheduler(
                    group, self.mirror, self.queue, delay=0
                )
                group.create_task(write_worker(self.queue, schedule))
                await self.queue.put(_noop)

        # then
        self.assertIsInstance(cm.exception.exceptions[0], RuntimeError)

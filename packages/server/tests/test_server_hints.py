"""The hint stage: one ordered consumer per backend, and delayed work."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server._services.sync.writer import WriterStoppedError
from wcpan.drive.server.types import ChangeHandler, HintStage


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


class _Clock:
    """Delays that end only when the test says so."""

    def __init__(self) -> None:
        self.sleeping: list[tuple[float, asyncio.Future[None]]] = []
        self.changed = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        entry = (delay, future)
        self.sleeping.append(entry)
        self.changed.set()
        try:
            await future
        finally:
            self.sleeping.remove(entry)
            self.changed.set()

    async def until_sleeping(self, count: int) -> None:
        async with asyncio.timeout(5):
            while len(self.sleeping) != count:
                self.changed.clear()
                await self.changed.wait()

    def fire_oldest(self) -> None:
        self.sleeping[0][1].set_result(None)


class _Handler(ChangeHandler[str]):
    def __init__(self, *, continue_after_failure: bool = False) -> None:
        self.continue_after_failure = continue_after_failure
        self.log: list[str] = []
        self.handled = asyncio.Event()
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.errors: dict[str, Exception] = {}
        self.on_handle: Callable[[str], Awaitable[None]] | None = None

    def gate(self, event: str) -> asyncio.Event:
        self.entered[event] = asyncio.Event()
        self.gates[event] = asyncio.Event()
        return self.gates[event]

    async def handle(self, event: str) -> None:
        self.log.append(f"start:{event}")
        if event in self.gates:
            self.entered[event].set()
            await self.gates[event].wait()
        if self.on_handle is not None:
            await self.on_handle(event)
        if event in self.errors:
            raise self.errors[event]
        self.log.append(f"end:{event}")
        self.handled.set()


class _HintTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.hints = HintStage(sleep=self.clock.sleep)
        self.tasks: list[asyncio.Task[None]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    def start(self, handler: _Handler) -> asyncio.Task[None]:
        task = asyncio.create_task(self.hints.run(handler))
        self.tasks.append(task)
        return task

    async def drained(self) -> None:
        async with asyncio.timeout(5):
            await self.hints.join()

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)


class ConsumerTest(_HintTestCase):
    async def test_events_published_before_the_consumer_starts_are_held(self) -> None:
        # given
        handler = _Handler()
        self.hints.publish("a")
        await self.settle()
        held = list(handler.log)
        # when
        self.start(handler)
        await self.drained()
        # then
        self.assertEqual(held, [])
        self.assertEqual(handler.log, ["start:a", "end:a"])

    async def test_events_run_one_at_a_time_in_order(self) -> None:
        # given
        handler = _Handler()
        release = handler.gate("a")
        self.start(handler)
        self.hints.publish("a")
        self.hints.publish("b")
        await handler.entered["a"].wait()
        await self.settle()
        during = list(handler.log)
        # when
        release.set()
        await self.drained()
        # then
        self.assertEqual(during, ["start:a"])
        self.assertEqual(handler.log, ["start:a", "end:a", "start:b", "end:b"])

    async def test_join_waits_for_the_running_event(self) -> None:
        # given: the queue is empty, but the event is still being handled
        handler = _Handler()
        release = handler.gate("a")
        self.start(handler)
        self.hints.publish("a")
        await handler.entered["a"].wait()
        joined = asyncio.create_task(self.drained())
        await self.settle()
        # when
        finished_early = joined.done()
        release.set()
        await joined
        # then
        self.assertFalse(finished_early)

    async def test_failure_stops_the_consumer_by_default(self) -> None:
        # given
        handler = _Handler()
        handler.errors["a"] = ValueError("bad event")
        task = self.start(handler)
        # when
        self.hints.publish("a")
        self.hints.publish("b")
        # then
        with self.assertRaisesRegex(ValueError, "bad event"):
            async with asyncio.timeout(5):
                await task
        self.assertNotIn("start:b", handler.log)

    async def test_failure_can_be_survived(self) -> None:
        # given
        handler = _Handler(continue_after_failure=True)
        handler.errors["a"] = ValueError("bad event")
        self.start(handler)
        # when
        self.hints.publish("a")
        self.hints.publish("b")
        await self.drained()
        # then
        self.assertIn("end:b", handler.log)

    async def test_stopped_writer_is_never_survived(self) -> None:
        # given
        handler = _Handler(continue_after_failure=True)
        handler.errors["a"] = WriterStoppedError("gone")
        task = self.start(handler)
        # when
        self.hints.publish("a")
        # then
        with self.assertRaises(WriterStoppedError):
            async with asyncio.timeout(5):
                await task


class LaterTest(_HintTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

    def call(self, name: str) -> Callable[[], Awaitable[None]]:
        async def work() -> None:
            self.calls.append(name)

        return work

    async def running(self, handler: _Handler | None = None) -> _Handler:
        handler = handler or _Handler()
        self.start(handler)
        await self.settle()
        return handler

    async def test_runs_after_its_delay(self) -> None:
        # given
        await self.running()
        # when
        self.hints.later("k", 10.0, self.call("first"))
        await self.clock.until_sleeping(1)
        before = list(self.calls)
        self.clock.fire_oldest()
        await self.settle()
        # then
        self.assertEqual(self.clock.sleeping, [])
        self.assertEqual(before, [])
        self.assertEqual(self.calls, ["first"])

    async def test_newer_call_replaces_a_pending_timer(self) -> None:
        # given
        await self.running()
        self.hints.later("k", 10.0, self.call("first"))
        await self.clock.until_sleeping(1)
        # when
        self.hints.later("k", 10.0, self.call("second"))
        await self.settle()
        await self.clock.until_sleeping(1)
        self.clock.fire_oldest()
        await self.settle()
        # then
        self.assertEqual(self.calls, ["second"])

    async def test_keys_are_independent(self) -> None:
        # given
        await self.running()
        # when
        self.hints.later("a", 1.0, self.call("a"))
        self.hints.later("b", 2.0, self.call("b"))
        # then
        await self.clock.until_sleeping(2)

    async def test_newer_call_does_not_cancel_running_work(self) -> None:
        # given
        await self.running()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow() -> None:
            entered.set()
            await release.wait()
            self.calls.append("slow")

        self.hints.later("k", 1.0, slow)
        await self.clock.until_sleeping(1)
        self.clock.fire_oldest()
        await entered.wait()
        # when
        self.hints.later("k", 1.0, self.call("next"))
        await self.clock.until_sleeping(1)
        release.set()
        await self.settle()
        # then
        self.assertEqual(self.calls, ["slow"])

    async def test_does_not_block_the_consumer_or_count_for_join(self) -> None:
        # given
        handler = await self.running()
        self.hints.later("k", 1.0, self.call("late"))
        await self.clock.until_sleeping(1)
        # when
        self.hints.publish("a")
        await self.drained()
        # then
        self.assertIn("end:a", handler.log)
        self.assertEqual(self.calls, [])

    async def test_can_be_scheduled_from_a_handler(self) -> None:
        # given
        handler = _Handler()

        async def on_handle(event: str) -> None:
            self.hints.later(event, 1.0, self.call(event))

        handler.on_handle = on_handle
        await self.running(handler)
        # when
        self.hints.publish("a")
        await self.drained()
        await self.clock.until_sleeping(1)
        self.clock.fire_oldest()
        await self.settle()
        # then
        self.assertEqual(self.calls, ["a"])

    async def test_failure_follows_the_handler_policy(self) -> None:
        # given
        handler = _Handler()
        task = self.start(handler)
        await self.settle()

        async def fail() -> None:
            raise ValueError("late failure")

        self.hints.later("k", 1.0, fail)
        await self.clock.until_sleeping(1)
        # when
        self.clock.fire_oldest()
        # then
        with self.assertRaisesRegex(ValueError, "late failure"):
            async with asyncio.timeout(5):
                await task

    async def test_survivable_failure_keeps_the_consumer_running(self) -> None:
        # given
        handler = await self.running(_Handler(continue_after_failure=True))

        async def fail() -> None:
            raise ValueError("late failure")

        self.hints.later("k", 1.0, fail)
        await self.clock.until_sleeping(1)
        self.clock.fire_oldest()
        await self.settle()
        # when
        self.hints.publish("a")
        await self.drained()
        # then
        self.assertIn("end:a", handler.log)

    async def test_stopping_the_consumer_cancels_timers(self) -> None:
        # given
        handler = _Handler()
        task = self.start(handler)
        await self.settle()
        self.hints.later("k", 1.0, self.call("late"))
        await self.clock.until_sleeping(1)
        # when
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # then
        self.assertEqual(self.clock.sleeping, [])
        self.assertEqual(self.calls, [])

    async def test_needs_a_running_consumer(self) -> None:
        # when, then
        with self.assertRaises(RuntimeError):
            self.hints.later("k", 1.0, self.call("late"))

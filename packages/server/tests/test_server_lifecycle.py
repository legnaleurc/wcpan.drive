"""The order in which the core starts one backend instance."""

import asyncio
from contextlib import suppress
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from wcpan.drive.server._backend import BackendLifecycle
from wcpan.drive.server.types import ChangeHandler, CoreServices


class _Handler(ChangeHandler[str]):
    def __init__(self) -> None:
        self.handled: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, event: str) -> None:
        self.entered.set()
        await self.release.wait()
        self.handled.append(event)


class _Runtime:
    def __init__(self, handler: _Handler | None) -> None:
        self.handler = handler
        self.scan_release = asyncio.Event()
        self.scan_error: Exception | None = None
        self.run_error: Exception | None = None
        self.running = asyncio.Event()

    async def startup_scan(self) -> None:
        await self.scan_release.wait()
        if self.scan_error is not None:
            raise self.scan_error

    def change_handler(self) -> _Handler | None:
        return self.handler

    async def run(self) -> None:
        self.running.set()
        if self.run_error is not None:
            raise self.run_error
        await asyncio.Event().wait()


class LifecycleTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.drain_release = asyncio.Event()
        self.drain_release.set()
        writer: Any = MagicMock()
        writer.wait_enrichment_drained = AsyncMock(side_effect=self.drain_release.wait)
        self.core = CoreServices(
            mirror=MagicMock(),
            writer=writer,
            ids=MagicMock(),
            media=MagicMock(),
        )
        self.tasks: list[asyncio.Task[None]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    def start(self, runtime: _Runtime) -> tuple[asyncio.Task[None], asyncio.Task[None]]:
        lifecycle = BackendLifecycle(runtime, self.core)
        ready = asyncio.create_task(lifecycle.until_ready())
        running = asyncio.create_task(lifecycle.run())
        self.tasks += [ready, running]
        return ready, running

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_nothing_runs_before_the_startup_scan_ends(self) -> None:
        # given
        handler = _Handler()
        runtime = _Runtime(handler)
        self.start(runtime)
        self.core.hints.publish("event")
        await self.settle()
        # when
        started_early = runtime.running.is_set() or handler.entered.is_set()
        runtime.scan_release.set()
        # then
        async with asyncio.timeout(5):
            await runtime.running.wait()
            await handler.entered.wait()
        self.assertFalse(started_early)

    async def test_ready_waits_for_events_published_before_it(self) -> None:
        # given
        handler = _Handler()
        runtime = _Runtime(handler)
        runtime.scan_release.set()
        self.core.hints.publish("event")
        ready, _ = self.start(runtime)
        async with asyncio.timeout(5):
            await handler.entered.wait()
        await self.settle()
        # when
        ready_while_handling = ready.done()
        handler.release.set()
        async with asyncio.timeout(5):
            await ready
        # then
        self.assertFalse(ready_while_handling)
        self.assertEqual(handler.handled, ["event"])

    async def test_ready_waits_for_enrichment(self) -> None:
        # given
        self.drain_release.clear()
        runtime = _Runtime(None)
        runtime.scan_release.set()
        ready, _ = self.start(runtime)
        await self.settle()
        # when
        ready_while_enriching = ready.done()
        self.drain_release.set()
        async with asyncio.timeout(5):
            await ready
        # then
        self.assertFalse(ready_while_enriching)

    async def test_backend_without_a_handler_becomes_ready(self) -> None:
        # given
        runtime = _Runtime(None)
        runtime.scan_release.set()
        # when
        ready, _ = self.start(runtime)
        # then
        async with asyncio.timeout(5):
            await ready
            await runtime.running.wait()

    async def test_stray_hint_without_a_handler_does_not_block_ready(self) -> None:
        # given
        runtime = _Runtime(None)
        runtime.scan_release.set()
        self.core.hints.publish("event")
        # when
        ready, _ = self.start(runtime)
        # then
        async with asyncio.timeout(5):
            await ready

    async def test_failed_startup_scan_fails_readiness(self) -> None:
        # given
        runtime = _Runtime(None)
        runtime.scan_error = ValueError("no source")
        runtime.scan_release.set()
        # when
        ready, _ = self.start(runtime)
        # then
        with self.assertRaisesRegex(ValueError, "no source"):
            async with asyncio.timeout(5):
                await ready

    async def test_failed_event_stops_the_backend(self) -> None:
        # given
        handler = _Handler()
        handler.release.set()
        runtime = _Runtime(handler)
        runtime.scan_release.set()

        async def fail(_: str) -> None:
            raise ValueError("bad event")

        handler.handle = fail  # type: ignore[method-assign]
        _, running = self.start(runtime)
        # when
        self.core.hints.publish("event")
        # then
        with self.assertRaises(ExceptionGroup) as raised:
            async with asyncio.timeout(5):
                await running
        self.assertTrue(raised.exception.subgroup(ValueError))

    async def test_failed_background_work_stops_the_backend(self) -> None:
        # given
        runtime = _Runtime(_Handler())
        runtime.run_error = ValueError("worker failed")
        runtime.scan_release.set()
        # when
        _, running = self.start(runtime)
        # then
        with self.assertRaises(ExceptionGroup) as raised:
            async with asyncio.timeout(5):
                await running
        self.assertTrue(raised.exception.subgroup(ValueError))

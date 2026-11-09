"""Tests for executor calls that survive the caller's cancellation."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.lib.executor import allocate, run_to_completion


class _ExecutorTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = self.enterContext(ThreadPoolExecutor(max_workers=1))
        self.started = threading.Event()
        self.resume = threading.Event()

    def blocked(self, value: str) -> str:
        self.started.set()
        self.resume.wait()
        return value

    async def cancel_while_running(
        self, task: asyncio.Task[str], *, times: int = 1
    ) -> None:
        await asyncio.to_thread(self.started.wait)
        for _ in range(times):
            task.cancel()
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.resume.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestRunToCompletion(_ExecutorTestCase):
    async def test_returns_the_result(self) -> None:
        # when / then
        self.assertEqual(await run_to_completion(self.executor, str.upper, "a"), "A")

    async def test_cancelled_caller_waits_for_the_call(self) -> None:
        # given
        task = asyncio.create_task(run_to_completion(self.executor, self.blocked, "x"))
        # when / then
        await self.cancel_while_running(task)

    async def test_repeated_cancellation_still_waits(self) -> None:
        # given
        task = asyncio.create_task(run_to_completion(self.executor, self.blocked, "x"))
        # when / then
        await self.cancel_while_running(task, times=3)


class TestAllocate(_ExecutorTestCase):
    async def test_returns_the_allocation(self) -> None:
        # given
        released: list[str] = []
        # when
        value = await allocate(self.executor, lambda: "file", release=released.append)
        # then
        self.assertEqual(value, "file")
        self.assertEqual(released, [])

    async def test_cancelled_caller_releases_the_allocation(self) -> None:
        # given
        released: list[str] = []
        task = asyncio.create_task(
            allocate(
                self.executor, lambda: self.blocked("file"), release=released.append
            )
        )
        # when
        await self.cancel_while_running(task)
        # then
        self.assertEqual(released, ["file"])

    async def test_repeated_cancellation_still_releases(self) -> None:
        # given
        released: list[str] = []
        task = asyncio.create_task(
            allocate(
                self.executor, lambda: self.blocked("file"), release=released.append
            )
        )
        # when
        await self.cancel_while_running(task, times=3)
        # then
        self.assertEqual(released, ["file"])

    async def test_failed_allocation_releases_nothing(self) -> None:
        # given
        released: list[str] = []

        def fail() -> str:
            raise OSError("no space")

        # when / then
        with self.assertRaises(OSError):
            await allocate(self.executor, fail, release=released.append)
        self.assertEqual(released, [])

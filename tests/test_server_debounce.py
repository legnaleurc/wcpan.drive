"""Tests for server debounce helpers."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.lib.debounce import Debouncer, TaskIdDebouncer


class TestDebouncer(IsolatedAsyncioTestCase):
    async def test_runs_after_delay(self) -> None:
        seen: list[str] = []

        async def op() -> None:
            seen.append("ran")

        async with asyncio.TaskGroup() as group:
            debouncer = Debouncer(group, 0.01)
            debouncer.start(op)
            await asyncio.sleep(0.03)

        self.assertEqual(seen, ["ran"])

    async def test_restart_pending_task_runs_latest_factory(self) -> None:
        seen: list[str] = []

        async def first() -> None:
            seen.append("first")

        async def second() -> None:
            seen.append("second")

        async with asyncio.TaskGroup() as group:
            debouncer = Debouncer(group, 0.03)
            debouncer.start(first)
            await asyncio.sleep(0.01)
            debouncer.start(second)
            await asyncio.sleep(0.05)

        self.assertEqual(seen, ["second"])

    async def test_start_after_body_starts_keeps_running_task(self) -> None:
        seen: list[str] = []
        started = asyncio.Event()
        finish = asyncio.Event()

        async def first() -> None:
            seen.append("first")
            started.set()
            await finish.wait()

        async def second() -> None:
            seen.append("second")

        async with asyncio.TaskGroup() as group:
            debouncer = Debouncer(group, 0.01)
            debouncer.start(first)
            await started.wait()
            debouncer.start(second)
            finish.set()
            await asyncio.sleep(0.03)

        self.assertEqual(seen, ["first", "second"])

    async def test_cancel_removes_pending_task(self) -> None:
        seen: list[str] = []

        async def op() -> None:
            seen.append("ran")

        async with asyncio.TaskGroup() as group:
            debouncer = Debouncer(group, 0.01)
            debouncer.start(op)
            debouncer.cancel_pending()
            await asyncio.sleep(0.03)

        self.assertEqual(seen, [])


class TestTaskIdDebouncer(IsolatedAsyncioTestCase):
    async def test_debounces_each_key_independently(self) -> None:
        seen: list[str] = []

        async def first_a() -> None:
            seen.append("first-a")

        async def second_a() -> None:
            seen.append("second-a")

        async def first_b() -> None:
            seen.append("first-b")

        async with asyncio.TaskGroup() as group:
            debouncer = TaskIdDebouncer(group, 0.03)
            debouncer.schedule("a", first_a)
            debouncer.schedule("b", first_b)
            await asyncio.sleep(0.01)
            debouncer.schedule("a", second_a)
            await asyncio.sleep(0.05)

        self.assertEqual(seen, ["first-b", "second-a"])

    async def test_schedule_after_body_starts_schedules_later_work(self) -> None:
        seen: list[str] = []
        started = asyncio.Event()
        finish = asyncio.Event()

        async def first() -> None:
            seen.append("first")
            started.set()
            await finish.wait()

        async def second() -> None:
            seen.append("second")

        async with asyncio.TaskGroup() as group:
            debouncer = TaskIdDebouncer(group, 0.01)
            debouncer.schedule("a", first)
            await started.wait()
            debouncer.schedule("a", second)
            finish.set()
            await asyncio.sleep(0.03)

        self.assertEqual(seen, ["first", "second"])

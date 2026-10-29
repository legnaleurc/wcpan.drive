"""Tests for debounce helpers."""

import asyncio
import logging
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.lib.debounce import Debouncer, TaskIdDebouncer


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


class TestDebouncer(IsolatedAsyncioTestCase):
    async def test_start_returns_without_blocking(self):
        # given
        completed = asyncio.Event()

        async def work():
            completed.set()

        async with asyncio.TaskGroup() as tg:
            debouncer = Debouncer(tg, 999)
            # when
            debouncer.start(lambda: work())
            await asyncio.sleep(0)
            # then
            self.assertFalse(completed.is_set())
            debouncer.cancel_pending()

    async def test_start_runs_after_delay(self):
        # given
        completed = asyncio.Event()

        async def work():
            completed.set()

        async with asyncio.TaskGroup() as tg:
            debouncer = Debouncer(tg, 0)
            # when
            debouncer.start(lambda: work())
            await completed.wait()

        # then
        self.assertFalse(debouncer.cancel_pending())

    async def test_cancel_pending_cancels_unstarted_task(self):
        # given
        async with asyncio.TaskGroup() as tg:
            debouncer = Debouncer(tg, 999)
            # when
            task = debouncer.start(lambda: asyncio.sleep(0))

            # then
            self.assertTrue(debouncer.cancel_pending())
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_cancel_pending_ignores_started_task(self):
        # given
        first_started = asyncio.Event()
        allow_finish = asyncio.Event()
        cancelled = False

        async def work():
            nonlocal cancelled
            first_started.set()
            try:
                await allow_finish.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise

        async with asyncio.TaskGroup() as tg:
            debouncer = Debouncer(tg, 0)
            debouncer.start(lambda: work())
            await first_started.wait()

            # when / then
            self.assertFalse(debouncer.cancel_pending())
            allow_finish.set()

        # then
        self.assertFalse(cancelled)

    async def test_body_exception_propagates_through_task_group(self):
        # given
        async def work():
            raise ValueError("oops")

        # when
        with self.assertRaises(ExceptionGroup) as cm:
            async with asyncio.TaskGroup() as tg:
                debouncer = Debouncer(tg, 0)
                debouncer.start(lambda: work())
                await asyncio.sleep(0)

        # then
        self.assertIsInstance(cm.exception.exceptions[0], ValueError)


class TestTaskIdDebouncer(IsolatedAsyncioTestCase):
    async def test_schedule_returns_without_blocking(self):
        # given
        completed = asyncio.Event()

        async def work():
            completed.set()

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=999)
            # when
            svc.schedule("f1", lambda: work())
            await asyncio.sleep(0)
            # then
            self.assertFalse(completed.is_set())
            svc.cancel("f1")

    async def test_schedule_creates_task_after_delay(self):
        # given
        completed = asyncio.Event()

        async def work():
            completed.set()

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0)
            # when
            svc.schedule("f1", lambda: work())
            # then
            await completed.wait()

    async def test_schedule_replaces_existing_before_delay(self):
        # given
        first_called = False

        async def first():
            nonlocal first_called
            first_called = True

        second_done = asyncio.Event()

        async def second():
            second_done.set()

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0.05)
            svc.schedule("f1", lambda: first())
            await asyncio.sleep(0)
            # when
            # Scheduling same file_id during the delay should renew the debounce.
            svc.schedule("f1", lambda: second())
            await second_done.wait()

        # then
        self.assertFalse(first_called)

    async def test_schedule_after_body_starts_schedules_later_work(self):
        # given
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()
        first_cancelled = False
        second_called = False

        async def first():
            nonlocal first_cancelled
            first_started.set()
            try:
                await allow_first_finish.wait()
            except asyncio.CancelledError:
                first_cancelled = True
                raise

        async def second():
            nonlocal second_called
            second_called = True

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0)
            svc.schedule("f1", lambda: first())
            await first_started.wait()
            # when
            svc.schedule("f1", lambda: second())
            await asyncio.sleep(0)
            allow_first_finish.set()

        # then
        self.assertFalse(first_cancelled)
        self.assertTrue(second_called)

    async def test_cancel_removes_task(self):
        # given
        called = False

        async def work():
            nonlocal called
            called = True

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=999)
            svc.schedule("f1", lambda: work())
            await asyncio.sleep(0)
            # when
            svc.cancel("f1")

        # then
        self.assertFalse(called)

    async def test_cancel_nonexistent_is_noop(self):
        # given
        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0)
            # when / then
            svc.cancel("no-such-file")  # should not raise

    async def test_done_callback_cleans_up(self):
        # given
        done = asyncio.Event()

        async def quick():
            done.set()

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0)
            # when
            svc.schedule("f1", lambda: quick())
            await done.wait()
            # Give the done-callback a chance to run
            await asyncio.sleep(0)
            # then
            self.assertNotIn("f1", svc._debouncers)

    async def test_old_done_callback_does_not_remove_replacement(self):
        # given
        first_called = False
        second_done = asyncio.Event()

        async def first():
            nonlocal first_called
            first_called = True

        async def second():
            second_done.set()

        async with asyncio.TaskGroup() as tg:
            svc = TaskIdDebouncer(tg, delay=0.05)
            svc.schedule("f1", lambda: first())
            old_task = svc._debouncers["f1"]._timer
            self.assertIsNotNone(old_task)
            # when
            svc.schedule("f1", lambda: second())
            # then
            with self.assertRaises(asyncio.CancelledError):
                await old_task
            self.assertIn("f1", svc._debouncers)
            await second_done.wait()

        self.assertFalse(first_called)

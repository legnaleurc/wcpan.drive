"""MediaRetries: backoff timers for transient probe failures."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server._services.media.retries import MediaRetries


class TestMediaRetries(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.retries = MediaRetries(first_delay=0.01, max_delay=0.04)
        self.calls: list[str] = []
        self.called = asyncio.Event()

        async def retry(source_key: str) -> None:
            self.calls.append(source_key)
            self.called.set()

        await self.enterAsyncContext(self.retries.register("b", retry))

    async def test_failure_schedules_a_retry(self) -> None:
        # when
        delay = self.retries.failed("b", "k")
        # then
        self.assertEqual(delay, 0.01)
        async with asyncio.timeout(5):
            await self.called.wait()
        self.assertEqual(self.calls, ["k"])

    async def test_delay_doubles_up_to_the_cap(self) -> None:
        # when
        delays = [self.retries.failed("b", "k") for _ in range(4)]
        # then
        self.assertEqual(delays, [0.01, 0.02, 0.04, 0.04])

    async def test_attempt_cancels_the_pending_timer(self) -> None:
        # given
        self.retries.failed("b", "k")
        # when
        self.retries.attempting("b", "k")
        await asyncio.sleep(0.05)
        # then
        self.assertEqual(self.calls, [])
        self.assertEqual(self.retries.pending("b"), set())

    async def test_attempt_keeps_the_backoff(self) -> None:
        # given
        self.retries.failed("b", "k")
        self.retries.attempting("b", "k")
        # when
        delay = self.retries.failed("b", "k")
        # then
        self.assertEqual(delay, 0.02)

    async def test_forget_resets_the_backoff(self) -> None:
        # given
        self.retries.failed("b", "k")
        # when
        self.retries.forget("b", "k")
        # then
        self.assertEqual(self.retries.pending("b"), set())
        self.assertEqual(self.retries.failed("b", "k"), 0.01)

    async def test_unregistered_backend_is_not_retried(self) -> None:
        # when / then
        self.assertIsNone(self.retries.failed("other", "k"))

    async def test_failed_attempt_is_rescheduled(self) -> None:
        # given
        retries = MediaRetries(first_delay=0.01)
        attempts: list[str] = []
        second = asyncio.Event()

        async def retry(source_key: str) -> None:
            attempts.append(source_key)
            if len(attempts) == 1:
                raise RuntimeError("cannot tell now")
            second.set()

        async with retries.register("c", retry):
            # when
            with self.assertLogs("wcpan.drive.server._services.media.retries"):
                retries.failed("c", "k")
                async with asyncio.timeout(5):
                    await second.wait()
        # then
        self.assertEqual(attempts, ["k", "k"])

    async def test_leaving_cancels_timers_and_running_attempts(self) -> None:
        # given
        retries = MediaRetries(first_delay=0.01)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def retry(source_key: str) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async with retries.register("c", retry):
            retries.failed("c", "running")
            async with asyncio.timeout(5):
                await started.wait()
            retries.failed("c", "waiting")
            # when: the backend stops serving
        # then
        self.assertTrue(cancelled.is_set())
        self.assertEqual(retries.pending("c"), set())

    async def test_timer_firing_during_its_attempt_runs_again_after_it(
        self,
    ) -> None:
        # given: the attempt fails the file again and keeps running past the
        # delay, as a folder scan does
        retries = MediaRetries(first_delay=0.01)
        attempts: list[str] = []
        second = asyncio.Event()

        async def retry(source_key: str) -> None:
            attempts.append(source_key)
            if len(attempts) == 1:
                retries.failed("c", source_key)
                await asyncio.sleep(0.1)
            else:
                second.set()

        async with retries.register("c", retry):
            # when
            retries.failed("c", "k")
            async with asyncio.timeout(5):
                await second.wait()
        # then
        self.assertEqual(attempts, ["k", "k"])

    async def test_retain_forgets_pruned_files(self) -> None:
        # given
        self.retries.failed("b", "kept")
        self.retries.failed("b", "pruned")
        # when
        self.retries.retain("b", {"kept"})
        # then
        self.assertEqual(self.retries.pending("b"), {"kept"})
        self.assertEqual(self.retries.failed("b", "pruned"), 0.01)

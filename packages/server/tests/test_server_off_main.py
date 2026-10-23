"""Tests for OffMainService executor bridge."""

from concurrent.futures import ProcessPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.services.off_main import OffMainService


def _triple(x: int) -> int:
    return x * 3


def _concat(a: str, b: str) -> str:
    return a + b


class TestOffMainService(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._pool = ProcessPoolExecutor(max_workers=2)
        self._off = OffMainService(pool=self._pool)

    async def asyncTearDown(self) -> None:
        self._pool.shutdown(wait=True)

    async def test_runs_callable_on_executor(self):
        # when
        result = await self._off(_triple, 4)
        # then
        self.assertEqual(result, 12)

    async def test_untimed_runs_on_executor(self):
        # when
        result = await self._off.untimed(_concat, "foo", "bar")
        # then
        self.assertEqual(result, "foobar")

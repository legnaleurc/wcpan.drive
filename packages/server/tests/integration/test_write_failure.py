"""A write worker that fails takes the whole server down."""

import asyncio
import os
import signal
import tempfile
from unittest import IsolatedAsyncioTestCase

from .._media import requires_mediainfo
from ._server_process import start_server


@requires_mediainfo
class TestWriteFailureStopsTheServer(IsolatedAsyncioTestCase):
    async def serve_until_exit(self, phase: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            process = await start_server(tmp, phase=phase)
            try:
                async with asyncio.timeout(30):
                    output, _ = await process.communicate()
            except TimeoutError:
                # The whole group: a killed server leaves its worker pool behind.
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                raise
            assert process.returncode is not None
            return process.returncode, output.decode()

    async def test_server_exits_with_the_write_error(self) -> None:
        for phase in ("startup", "serving", "draining"):
            with self.subTest(phase=phase):
                # when
                code, output = await self.serve_until_exit(phase)
                # then
                self.assertNotEqual(code, 0)
                self.assertIn('near "NOT"', output)

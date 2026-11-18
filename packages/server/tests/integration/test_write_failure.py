"""A write worker that fails takes the whole server down."""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server._config import CONFIG_VERSION

from .._media import requires_mediainfo


_PACKAGE = Path(__file__).parent.parent.parent
_START = "from wcpan.drive.server._main import main; main()"
_FACTORY = "tests.integration._failing_backend.create_backend"


@requires_mediainfo
class TestWriteFailureStopsTheServer(IsolatedAsyncioTestCase):
    async def serve_until_exit(self, phase: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "server.yaml"
            config.write_text(
                f"version: {CONFIG_VERSION}\n"
                "server:\n"
                f"  database_url: {tmp}/mirror.sqlite\n"
                "  host: 127.0.0.1\n"
                "  port: 0\n"
                "backends:\n"
                "  failing:\n"
                f"    type: {_FACTORY}\n"
                f"    phase: {phase}\n"
                "mounts:\n"
                "  docs:\n"
                "    backend: failing\n"
                "    source: docs\n"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _START,
                "--config",
                str(config),
                "serve",
                cwd=_PACKAGE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
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

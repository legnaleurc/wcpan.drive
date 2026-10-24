"""server.example.yaml stays loadable with the backends it names."""

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.app import entered_backends, managed_core, managed_off_main
from wcpan.drive.server.config import load_config
from wcpan.drive.server.workers import noop_after_write


_EXAMPLE = Path(__file__).parents[1] / "server.example.yaml"


class TestExampleConfig(IsolatedAsyncioTestCase):
    async def test_example_enters_every_backend_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                load_config(_EXAMPLE), database_url=str(Path(tmp) / "mirror.db")
            )
            async with (
                managed_off_main() as off_main,
                managed_core(
                    config, off_main=off_main, after_write=noop_after_write
                ) as core,
                entered_backends(config, core, offline=True) as backends,
            ):
                self.assertEqual(set(backends), set(config.backends))
            await asyncio.sleep(0)

"""The generic app composes a backend through the ``Backend`` contract."""

import asyncio
import os
import sqlite3
import tempfile
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.protocol.types import MirrorStableId
from wcpan.drive.server.app import create_app
from wcpan.drive.server.backend import CoreServices
from wcpan.drive.server.keys import NODE_OPERATIONS_KEY
from wcpan.drive.server.types import BackendLocator, ServerConfig


class _FakeSchema:
    def ensure_schema(self, con: sqlite3.Connection) -> None:
        con.execute("CREATE TABLE IF NOT EXISTS fake_backend (x INTEGER)")

    def bind_locator(
        self, con: sqlite3.Connection, node_id: MirrorStableId, locator: BackendLocator
    ) -> None:
        pass

    def forget_nodes(self, con: sqlite3.Connection, node_ids: Sequence[str]) -> None:
        pass

    def forget_orphans(self, con: sqlite3.Connection) -> None:
        pass


class _FakeRuntime:
    def __init__(self) -> None:
        self.operations = MagicMock()
        self.uploader = MagicMock()
        self.discovered = asyncio.Event()
        self.running = asyncio.Event()

    async def run_until_ready(self) -> None:
        await self.discovered.wait()

    async def run(self) -> None:
        self.running.set()
        await asyncio.Event().wait()


class _FakeBackend:
    def __init__(self) -> None:
        self.runtime = _FakeRuntime()
        self.core: CoreServices | None = None

    @property
    def schema(self) -> _FakeSchema:
        return _FakeSchema()

    def add_routes(self, app: web.Application) -> None:
        async def hook(_request: web.Request) -> web.Response:
            return web.Response(text="backend")

        app.router.add_post("/api/v1/fake-hook", hook)

    @asynccontextmanager
    async def start(
        self, app: web.Application, core: CoreServices
    ) -> AsyncGenerator[_FakeRuntime, None]:
        self.core = core
        yield self.runtime


class TestCreateApp(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.backend = _FakeBackend()
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            database_url=self.db_path,
            mounts={},
            local_paths={},
        )
        self.app = create_app(config, backend=self.backend)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.unlink(self.db_path + suffix)

    async def test_backend_is_started_and_serves_after_discovery(self) -> None:
        async with asyncio.timeout(5):
            await self.backend.runtime.running.wait()
        self.assertIsNotNone(self.backend.core)
        self.assertIs(self.app[NODE_OPERATIONS_KEY], self.backend.runtime.operations)

        resp = await self.client.get("/readyz")
        self.assertEqual(resp.status, 503)

        self.backend.runtime.discovered.set()
        async with asyncio.timeout(5):
            while (await self.client.get("/readyz")).status != 200:
                await asyncio.sleep(0.01)

        resp = await self.client.post("/api/v1/fake-hook")
        self.assertEqual(await resp.text(), "backend")

        con = sqlite3.connect(self.db_path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        con.close()
        self.assertIn("fake_backend", tables)

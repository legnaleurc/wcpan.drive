"""The generic app composes a backend through the ``Backend`` contract."""

import asyncio
import os
import sqlite3
import tempfile
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.app import create_app
from wcpan.drive.server.keys import MIRROR_KEY, NODE_OPERATIONS_KEY
from wcpan.drive.server.services.operations import (
    OperationInvalidError,
    OperationNotSupportedError,
)
from wcpan.drive.server.types import BackendConfig, MountConfig, ServerConfig

from . import _fake_backend


_FACTORY = "tests._fake_backend.create_backend"


class TestCreateApp(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            database_url=self.db_path,
            backends={
                "fake": BackendConfig(type=_FACTORY, settings={}),
                "other": BackendConfig(type=_FACTORY, settings={}),
            },
            mounts={
                "docs": MountConfig(backend="fake", source="/docs"),
                "pics": MountConfig(backend="other", source="/pics"),
            },
        )
        self.app = create_app(config)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.backend = _fake_backend.instances["fake"]
        self.other = _fake_backend.instances["other"]

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.unlink(self.db_path + suffix)

    async def test_backend_is_started_and_serves_after_discovery(self) -> None:
        # when
        async with asyncio.timeout(5):
            await self.backend.running.wait()
        # then
        self.assertEqual(self.backend.context.mounts, {"docs": "/docs"})
        self.assertFalse(self.backend.context.offline)

        # when / then
        resp = await self.client.get("/readyz")
        self.assertEqual(resp.status, 503)

        # given
        await self.ready()

        # when / then
        resp = await self.client.post("/api/v1/backends/fake/hook")
        self.assertEqual(await resp.text(), "fake")
        resp = await self.client.post("/api/v1/backends/other/hook")
        self.assertEqual(await resp.text(), "other")
        resp = await self.client.post("/api/v1/backends/missing/hook")
        self.assertEqual(resp.status, 404)

        # then
        con = sqlite3.connect(self.db_path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        con.close()
        self.assertIn("fake_backend", tables)

    async def ready(self) -> None:
        self.backend.discovered.set()
        self.other.discovered.set()
        async with asyncio.timeout(5):
            while (await self.client.get("/readyz")).status != 200:
                await asyncio.sleep(0.01)

    async def test_ready_waits_for_every_backend(self) -> None:
        # when
        self.backend.discovered.set()
        await asyncio.sleep(0.05)
        # then
        self.assertEqual((await self.client.get("/readyz")).status, 503)
        # when
        self.other.discovered.set()
        # then
        async with asyncio.timeout(5):
            while (await self.client.get("/readyz")).status != 200:
                await asyncio.sleep(0.01)

    async def test_core_creates_root_and_mount_nodes(self) -> None:
        # when
        await self.ready()
        # then
        mirror = self.app[MIRROR_KEY]
        for node_id in ("_", "_docs", "_pics"):
            self.assertIsNotNone(await mirror.get_node_by_id(NodeId(node_id)))

    async def test_operations_go_to_the_mount_backend(self) -> None:
        # given
        record = MagicMock()
        record.id = "made"
        record.name = "x"
        for b in (self.backend, self.other):
            b.operations.create_folder = AsyncMock(
                side_effect=OperationInvalidError(b is self.other and "other" or "fake")
            )
        await self.ready()
        # when
        resp = await self.client.post(
            "/api/v1/nodes", json={"name": "x", "parent_id": "_pics"}
        )
        # then
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.reason, "other")
        self.backend.operations.create_folder.assert_not_awaited()

    async def test_unsupported_operation_is_forbidden(self) -> None:
        # given
        self.backend.operations.create_folder = AsyncMock(
            side_effect=OperationNotSupportedError("read-only")
        )
        await self.ready()

        # when
        resp = await self.client.post(
            "/api/v1/nodes", json={"name": "x", "parent_id": "_docs"}
        )

        # then
        self.assertEqual(resp.status, 403)

    async def test_invalid_operation_is_bad_request(self) -> None:
        # given
        self.backend.operations.create_folder = AsyncMock(
            side_effect=OperationInvalidError("bad name")
        )
        await self.ready()

        # when
        resp = await self.client.post(
            "/api/v1/nodes", json={"name": "x", "parent_id": "_docs"}
        )

        # then
        self.assertEqual(resp.status, 400)

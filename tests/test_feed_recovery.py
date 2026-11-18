"""client-feed recovers against the real server: resumable uploads and stale cursors.

The server runs in process with a filesystem mount. For uploads, a proxy between
the client and the server injects faults into the first chunk request: the
server accepts part of the bytes, then the client sees a 503 or a dropped
connection, and must resume from the offset the server reports. For cursors,
the server's history is replaced offline (``squash``), so the client must
rebuild its cache from a fresh replay.
"""

import asyncio
import hashlib
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal
from unittest import IsolatedAsyncioTestCase

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from wcpan.drive.client.api import create_drive
from wcpan.drive.client.feed import create_backend
from wcpan.drive.client.lib import upload_file_from_local
from wcpan.drive.client.types import Drive
from wcpan.drive.server._app import create_app
from wcpan.drive.server._config import parse_config
from wcpan.drive.server._services.mirror.maintenance import reset_change_history
from wcpan.drive.server._types import ServerConfig


# Headers that describe one hop, not the message.
_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

type Fault = Literal["busy", "drop"]


def _config(root: Path) -> ServerConfig:
    return parse_config(
        {
            "version": 1,
            "server": {"database_url": str(root / "mirror.sqlite")},
            "backends": {
                "disk": {
                    "type": "wcpan.drive.server.backend.filesystem.create_backend",
                    "watch": False,
                },
            },
            "mounts": {"docs": {"backend": "disk", "source": str(root / "docs")}},
        }
    )


@asynccontextmanager
async def _running(config: ServerConfig) -> AsyncGenerator[TestServer]:
    server = TestServer(create_app(config))
    await server.start_server()
    try:
        async with ClientSession() as session, asyncio.timeout(10):
            while True:
                async with session.get(server.make_url("/readyz")) as response:
                    if response.status == 200:
                        break
                await asyncio.sleep(0.02)
        yield server
    finally:
        await server.close()


@asynccontextmanager
async def _drive(url: str, cache: Path) -> AsyncGenerator[Drive]:
    async with create_drive(
        backend=partial(create_backend, server_url=url), cache_path=str(cache)
    ) as drive:
        yield drive


async def _sync(drive: Drive) -> None:
    async for _ in drive.sync():
        pass


class _FaultProxy:
    """Forwards requests to *upstream*; the first chunk request may fail.

    With a fault, only the first *partial* bytes of the first PATCH reach the
    server, as a complete chunk of that length, so the server holds them. The
    client then gets a 503 (``busy``) or a dropped connection (``drop``).
    """

    def __init__(self, upstream: TestServer, *, fault: Fault, partial: int) -> None:
        self._upstream = upstream
        self._fault: Fault | None = fault
        self._partial = partial
        # The Upload-Offset of every chunk request the client sent.
        self.offsets: list[int] = []
        self._session = ClientSession()

    async def close(self) -> None:
        await self._session.close()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._forward)
        return app

    async def _forward(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        fault = None
        if request.method == "PATCH":
            self.offsets.append(int(request.headers["Upload-Offset"]))
            fault, self._fault = self._fault, None
        if fault is not None:
            body = body[: self._partial]
        status, headers, content = await self._send(request, body)
        if fault == "busy":
            assert status == 204, status
            return web.Response(status=503, headers={"Retry-After": "0"})
        if fault == "drop":
            assert status == 204, status
            assert request.transport is not None
            request.transport.abort()
        return web.Response(status=status, headers=headers, body=content)

    async def _send(
        self, request: web.Request, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
        }
        async with self._session.request(
            request.method,
            self._upstream.make_url(request.rel_url),
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as response:
            content = await response.read()
            kept = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in _HOP_HEADERS
            }
            return response.status, kept, content


class TestResumableUploadRecovery(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "docs").mkdir()
        self.payload = bytes(range(256)) * 4096  # 1 MiB
        self.local = self.root / "upload.bin"
        self.local.write_bytes(self.payload)

    async def _upload_through(self, fault: Fault) -> list[int]:
        partial = 300_000
        async with _running(_config(self.root)) as server:
            proxy = _FaultProxy(server, fault=fault, partial=partial)
            self.addAsyncCleanup(proxy.close)
            front = TestServer(proxy.app())
            await front.start_server()
            self.addAsyncCleanup(front.close)

            async with _drive(
                str(front.make_url("/")), self.root / "cache.sqlite"
            ) as drive:
                await _sync(drive)
                docs = await drive.get_node_by_path(Path("/docs"))

                # when
                node = await upload_file_from_local(drive, self.local, docs)

        # then: one file with every byte, as the server reports it
        self.assertEqual((self.root / "docs/upload.bin").read_bytes(), self.payload)
        self.assertEqual(node.size, len(self.payload))
        self.assertEqual(node.hash, hashlib.md5(self.payload).hexdigest())
        self.assertEqual(
            sorted(p.name for p in (self.root / "docs").iterdir()), ["upload.bin"]
        )
        return proxy.offsets

    async def test_resumes_after_busy_from_server_offset(self) -> None:
        offsets = await self._upload_through("busy")
        self.assertEqual(offsets, [0, 300_000])

    async def test_resumes_after_dropped_connection_from_server_offset(self) -> None:
        offsets = await self._upload_through("drop")
        self.assertEqual(offsets, [0, 300_000])


class TestStaleCursorRecovery(IsolatedAsyncioTestCase):
    async def test_cache_is_rebuilt_from_replaced_history(self) -> None:
        # given: a client that synced a.txt and b.txt
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "docs").mkdir()
        (root / "docs/a.txt").write_bytes(b"a")
        (root / "docs/b.txt").write_bytes(b"b")
        config = _config(root)
        cache = root / "cache.sqlite"
        async with _running(config) as server:
            async with _drive(str(server.make_url("/")), cache) as drive:
                await _sync(drive)
                self.assertIsNotNone(await drive.get_node_by_path(Path("/docs/b.txt")))

        # given: b.txt is removed and c.txt added, then the history is replaced,
        # so it no longer mentions b.txt at all
        (root / "docs/b.txt").unlink()
        (root / "docs/c.txt").write_bytes(b"c")
        async with _running(config):
            pass
        reset_change_history(config.database_url)

        # when
        async with _running(config) as server:
            async with _drive(str(server.make_url("/")), cache) as drive:
                await _sync(drive)
                docs = await drive.get_node_by_path(Path("/docs"))
                names = sorted(n.name for n in await drive.get_children(docs))

        # then
        self.assertEqual(names, ["a.txt", "c.txt"])

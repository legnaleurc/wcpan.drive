"""Tests for shallow health probe and diagnostic handlers."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.server.handlers.health import (
    get_livez,
    get_readyz,
    put_null,
)
from wcpan.drive.server.keys import READY_KEY


def _make_app(*, ready: bool | None = None) -> web.Application:
    app = web.Application()
    if ready is not None:
        app[READY_KEY] = ready
    app.router.add_get("/livez", get_livez)
    app.router.add_get("/readyz", get_readyz)
    app.router.add_put("/null", put_null)
    return app


class TestLivez(IsolatedAsyncioTestCase):
    async def test_returns_ok(self) -> None:
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/livez")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body, {"ok": True})


class TestReadyz(IsolatedAsyncioTestCase):
    async def test_returns_ok_when_ready(self) -> None:
        app = _make_app(ready=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/readyz")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body, {"ok": True, "ready": True})

    async def test_returns_503_when_not_ready(self) -> None:
        app = _make_app(ready=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/readyz")
            self.assertEqual(resp.status, 503)
            body = await resp.json()
        self.assertEqual(body, {"ok": False, "ready": False})

    async def test_returns_503_when_ready_flag_missing(self) -> None:
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/readyz")
            self.assertEqual(resp.status, 503)
            body = await resp.json()
        self.assertEqual(body, {"ok": False, "ready": False})


class TestNullSink(IsolatedAsyncioTestCase):
    async def test_returns_stats_for_small_body(self) -> None:
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/null", data=b"hello")
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertEqual(body["bytes_received"], 5)
        self.assertIsInstance(body["elapsed_seconds"], float)
        self.assertGreaterEqual(body["elapsed_seconds"], 0.0)
        self.assertIsInstance(body["bytes_per_second"], float)
        self.assertGreaterEqual(body["bytes_per_second"], 0.0)
        self.assertIsInstance(body["mebibytes_per_second"], float)
        self.assertGreaterEqual(body["mebibytes_per_second"], 0.0)

    async def test_accepts_empty_body(self) -> None:
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/null", data=b"")
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertEqual(body["bytes_received"], 0)
        self.assertIsInstance(body["elapsed_seconds"], float)
        self.assertGreaterEqual(body["elapsed_seconds"], 0.0)

    async def test_works_when_ready_flag_is_false(self) -> None:
        app = _make_app(ready=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/null", data=b"abc")
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertEqual(body["bytes_received"], 3)

    async def test_consumes_streaming_request_body(self) -> None:
        app = _make_app()

        async def stream():
            yield b"ab"
            await asyncio.sleep(0)
            yield b"cde"
            await asyncio.sleep(0)
            yield b"f"

        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/null", data=stream())
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertEqual(body["bytes_received"], 6)

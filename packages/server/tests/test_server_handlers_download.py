"""Tests for the download handler: length, ranges, and backend failures."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase

from aiohttp import ClientPayloadError, ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server._handlers.nodes import download_node
from wcpan.drive.server._keys import MIRROR_KEY, NODE_OPERATIONS_KEY, READY_KEY
from wcpan.drive.server._services.operations import OperationUnavailableError


_CONTENT = b"0123456789"


def _make_node(size: int = len(_CONTENT)) -> NodeRecord:
    return NodeRecord(
        id="n1",
        parent_id="p1",
        name="x.bin",
        is_directory=False,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type="application/octet-stream",
        hash="",
        hash_algorithm="",
        size=size,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
    )


class _FakeMirror:
    def __init__(self, node: NodeRecord) -> None:
        self._node = node

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._node if node_id == self._node.id else None


class _FakeOperations:
    """Streams *content* in 2-byte chunks, failing as configured."""

    def __init__(
        self,
        *,
        content: bytes = _CONTENT,
        fail_on_open: bool = False,
        fail_after: int | None = None,
        fail_with: Exception | None = None,
        stop_after: int | None = None,
    ) -> None:
        self._content = content
        self._fail_on_open = fail_on_open
        self._fail_after = fail_after
        self._fail_with = fail_with or OperationUnavailableError("source went away")
        self._stop_after = stop_after
        self.requests: list[tuple[int, int]] = []
        # How each download context ended: None, or the exception it saw.
        self.exits: list[BaseException | None] = []

    @asynccontextmanager
    async def download(
        self, record: NodeRecord, *, offset: int, length: int
    ) -> AsyncGenerator[AsyncIterable[bytes], None]:
        if self._fail_on_open:
            raise OperationUnavailableError("backend is not running")
        self.requests.append((offset, length))
        try:
            yield self._chunks(offset, length)
        except BaseException as e:
            self.exits.append(e)
            raise
        self.exits.append(None)

    async def _chunks(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        # Like the filesystem backend: the requested positions, as far as the
        # content reaches.
        data = self._content[offset : offset + length]
        sent = 0
        for i in range(0, len(data), 2):
            if self._fail_after is not None and sent >= self._fail_after:
                raise self._fail_with
            if self._stop_after is not None and sent >= self._stop_after:
                return
            chunk = data[i : i + 2]
            sent += len(chunk)
            yield chunk
            await asyncio.sleep(0)


class TestDownloadNode(IsolatedAsyncioTestCase):
    def _make_app(
        self, operations: _FakeOperations, *, node: NodeRecord | None = None
    ) -> web.Application:
        app = web.Application()
        ready = asyncio.Event()
        ready.set()
        app[READY_KEY] = ready
        app[MIRROR_KEY] = _FakeMirror(node or _make_node())  # type: ignore[assignment]
        app[NODE_OPERATIONS_KEY] = operations  # type: ignore[assignment]
        app.router.add_get("/api/v1/nodes/{id}/download", download_node)
        return app

    async def _get(
        self,
        operations: _FakeOperations,
        range_: str | None = None,
        *,
        if_range: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        app = self._make_app(operations)
        headers = {} if range_ is None else {"Range": range_}
        if if_range is not None:
            headers["If-Range"] = if_range
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/nodes/n1/download", headers=headers)
            body = await resp.read()
        return resp.status, dict(resp.headers), body

    async def test_full_download_sends_its_length(self):
        # given
        operations = _FakeOperations()

        # when
        status, headers, body = await self._get(operations)

        # then
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(_CONTENT)))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", headers)
        self.assertEqual(body, _CONTENT)
        self.assertEqual(operations.requests, [(0, 10)])

    async def test_range_sends_its_length_and_content_range(self):
        # given
        operations = _FakeOperations()

        # when
        status, headers, body = await self._get(operations, "bytes=2-5")

        # then
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Length"], "4")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(body, b"2345")
        self.assertEqual(operations.requests, [(2, 4)])

    async def test_suffix_range(self):
        # given
        operations = _FakeOperations()

        # when
        status, headers, body = await self._get(operations, "bytes=-3")

        # then
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 7-9/10")
        self.assertEqual(body, b"789")
        self.assertEqual(operations.requests, [(7, 3)])

    async def test_open_ended_range(self):
        # when
        status, headers, body = await self._get(_FakeOperations(), "bytes=6-")

        # then
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Length"], "4")
        self.assertEqual(headers["Content-Range"], "bytes 6-9/10")
        self.assertEqual(body, b"6789")

    async def test_range_past_the_end_is_clipped(self):
        # when
        status, headers, body = await self._get(_FakeOperations(), "bytes=8-20")

        # then
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 8-9/10")
        self.assertEqual(body, b"89")

    async def test_range_outside_the_file_is_416(self):
        # given
        operations = _FakeOperations()

        # when
        status, headers, _ = await self._get(operations, "bytes=10-")

        # then
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */10")
        self.assertEqual(operations.requests, [])

    async def test_ranges_resolve_against_the_mirrored_size(self):
        # given: the source grew since it was mirrored
        operations = _FakeOperations(content=b"abcdefghijklmnop")

        # when
        status, headers, body = await self._get(operations, "bytes=-3")

        # then: the mirrored positions, not the last three bytes of the source
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 7-9/10")
        self.assertEqual(body, b"hij")

    async def test_head_is_answered_without_opening_the_source(self):
        # given
        operations = _FakeOperations()
        app = self._make_app(operations)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.head("/api/v1/nodes/n1/download")
            body = await resp.read()

        # then
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Length"], "10")
        self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
        self.assertEqual(body, b"")
        self.assertEqual(operations.requests, [])

    async def test_head_ignores_range(self):
        # RFC 9110 §14.2: GET is the only method with range semantics.
        for header in ("bytes=2-5", "bytes=10-", "bytes=-0"):
            with self.subTest(header=header):
                # given
                operations = _FakeOperations()
                app = self._make_app(operations)
                async with TestClient(TestServer(app)) as client:
                    # when
                    resp = await client.head(
                        "/api/v1/nodes/n1/download", headers={"Range": header}
                    )
                    body = await resp.read()

                # then
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Length"], "10")
                self.assertNotIn("Content-Range", resp.headers)
                self.assertEqual(body, b"")
                self.assertEqual(operations.requests, [])

    async def test_if_range_sends_the_whole_file(self):
        # RFC 9110 §13.1.5: no validator is sent, so If-Range never holds.
        for if_range in ('"abc"', "Thu, 01 Jan 1970 00:00:00 GMT"):
            for header in ("bytes=2-5", "bytes=10-"):
                with self.subTest(if_range=if_range, header=header):
                    # given
                    operations = _FakeOperations()

                    # when
                    status, headers, body = await self._get(
                        operations, header, if_range=if_range
                    )

                    # then
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Length"], "10")
                    self.assertNotIn("Content-Range", headers)
                    self.assertEqual(body, _CONTENT)
                    self.assertEqual(operations.requests, [(0, 10)])

    async def test_if_range_without_range_is_a_plain_get(self):
        # when
        status, headers, body = await self._get(_FakeOperations(), if_range='"abc"')

        # then
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Range", headers)
        self.assertEqual(body, _CONTENT)

    async def test_empty_file_is_sent_without_opening_the_source(self):
        # given
        operations = _FakeOperations()
        app = self._make_app(operations, node=_make_node(size=0))
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/nodes/n1/download")
            body = await resp.read()

        # then
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Length"], "0")
        self.assertEqual(body, b"")
        self.assertEqual(operations.requests, [])

    async def test_unavailable_before_streaming_is_503(self):
        # when
        status, _, _ = await self._get(_FakeOperations(fail_on_open=True))

        # then
        self.assertEqual(status, 503)

    async def test_failure_mid_stream_is_an_error_not_a_short_body(self):
        # given
        app = self._make_app(_FakeOperations(fail_after=4))
        with (
            self.assertLogs("wcpan.drive.server._handlers.nodes", "WARNING"),
            self.assertLogs("aiohttp.server", "ERROR"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/nodes/n1/download")
                # when / then
                with self.assertRaises(ClientPayloadError):
                    await resp.read()

    async def test_upstream_reset_mid_stream_is_an_error_not_a_disconnect(self):
        # given: the backend's own connection resets after the headers
        operations = _FakeOperations(fail_after=4, fail_with=ConnectionResetError())
        app = self._make_app(operations)
        with (
            self.assertLogs("wcpan.drive.server._handlers.nodes", "WARNING"),
            self.assertLogs("aiohttp.server", "ERROR"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/nodes/n1/download")
                # when / then
                with self.assertRaises(ClientPayloadError):
                    await resp.read()

    async def test_stream_ending_early_is_an_error_not_a_short_body(self):
        # given
        app = self._make_app(_FakeOperations(stop_after=4))
        with (
            self.assertLogs("wcpan.drive.server._handlers.nodes", "WARNING"),
            self.assertLogs("aiohttp.server", "ERROR"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/nodes/n1/download")
                # when / then
                with self.assertRaises(ClientPayloadError):
                    await resp.read()

    async def test_shrunk_source_is_an_error_not_a_short_body(self):
        # given: the source is shorter than its mirrored size
        app = self._make_app(_FakeOperations(content=b"01234"))
        with (
            self.assertLogs("wcpan.drive.server._handlers.nodes", "WARNING"),
            self.assertLogs("aiohttp.server", "ERROR"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/nodes/n1/download")
                # when / then
                with self.assertRaises(ClientPayloadError):
                    await resp.read()

    async def test_client_disconnect_ends_the_source_normally(self):
        # given: a large file, so the server is still writing when the client
        # goes; served like production, where a disconnect does not cancel the
        # handler (TestServer turns handler cancellation on)
        size = 64 * 1024 * 1024
        operations = _FakeOperations(content=b"x" * size)
        runner = web.AppRunner(self._make_app(operations, node=_make_node(size=size)))
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        async with ClientSession() as session:
            resp = await session.get(
                f"http://127.0.0.1:{port}/api/v1/nodes/n1/download"
            )
            await resp.content.readexactly(2)

            # when
            resp.close()
            for _ in range(1000):
                if operations.exits:
                    break
                await asyncio.sleep(0.01)

        # then: the source was closed as finished, not failed
        self.assertEqual(operations.exits, [None])

    async def test_invalid_ranges_are_ignored(self):
        for header in ("bytes=5-2", "bytes=0-1,3-4", "bytes=x"):
            with self.subTest(header=header):
                # when
                status, headers, body = await self._get(_FakeOperations(), header)
                # then
                self.assertEqual(status, 200)
                self.assertNotIn("Content-Range", headers)
                self.assertEqual(body, _CONTENT)

    async def test_zero_length_suffix_is_416(self):
        # when
        status, headers, _ = await self._get(_FakeOperations(), "bytes=-0")
        # then
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */10")

    async def test_failure_reason_names_only_the_exception_type(self):
        # given: upstream errors can carry URLs with credentials
        operations = _FakeOperations(
            fail_after=4, fail_with=ConnectionResetError("http://nas/?_sid=secret")
        )
        app = self._make_app(operations)
        with (
            self.assertLogs("wcpan.drive.server._handlers.nodes", "WARNING") as logs,
            self.assertLogs("aiohttp.server", "ERROR"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/nodes/n1/download")
                with self.assertRaises(ClientPayloadError):
                    await resp.read()
        # then
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertIn("ConnectionResetError", "\n".join(logs.output))

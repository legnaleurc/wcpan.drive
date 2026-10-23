"""ProbeSourceService: serving open media sources to local MediaInfo over HTTP."""

import asyncio
from collections.abc import AsyncGenerator
from unittest import IsolatedAsyncioTestCase
from urllib.parse import urlsplit

from aiohttp import ClientPayloadError, ClientSession
from wcpan.drive.server._services.media.probe.service import ProbeSourceService
from wcpan.drive.server._services.media.probe.source import MediaSourceError


_DATA = bytes(range(256)) * 1024


class _Source:
    """A MediaSource over bytes, in 4 KiB chunks, failing as configured."""

    def __init__(
        self,
        data: bytes = _DATA,
        *,
        fail_after: int | None = None,
        fail_with: Exception | None = None,
        stop_after: int | None = None,
        stall_after: int | None = None,
    ) -> None:
        self.data = data
        self._fail_after = fail_after
        self._fail_with = fail_with or MediaSourceError("connection reset")
        self._stop_after = stop_after
        self._stall_after = stall_after
        self.stalled = asyncio.Event()
        self.open_streams = 0
        self.streams: list[tuple[int, int]] = []

    @property
    def size(self) -> int:
        return len(self.data)

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        self.streams.append((offset, length))
        self.open_streams += 1
        try:
            sent = 0
            while sent < length:
                if self._fail_after is not None and sent >= self._fail_after:
                    raise self._fail_with
                if self._stop_after is not None and sent >= self._stop_after:
                    return
                if self._stall_after is not None and sent >= self._stall_after:
                    self.stalled.set()
                    await asyncio.Event().wait()
                n = min(4096, length - sent)
                yield self.data[offset + sent : offset + sent + n]
                sent += n
                await asyncio.sleep(0)
        finally:
            self.open_streams -= 1

    async def unchanged(self) -> bool:
        return True


class TestProbeSourceService(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = ProbeSourceService(read_timeout=0.2)
        await self.service.start()
        self.addAsyncCleanup(self.service.close)
        self.http = ClientSession()
        self.addAsyncCleanup(self.http.close)

    async def test_listens_on_loopback(self) -> None:
        async with self.service.session(_Source()) as session:
            # then
            self.assertEqual(urlsplit(session.url).hostname, "127.0.0.1")

    async def test_serves_the_whole_file_and_ranges(self) -> None:
        # given
        source = _Source()
        async with self.service.session(source) as session:
            # when
            async with self.http.get(session.url) as full:
                body = await full.read()
            async with self.http.get(
                session.url, headers={"Range": "bytes=100-199"}
            ) as part:
                part_body = await part.read()

            # then
            self.assertEqual(full.status, 200)
            self.assertEqual(body, _DATA)
            self.assertEqual(part.status, 206)
            self.assertEqual(
                part.headers["Content-Range"], f"bytes 100-199/{len(_DATA)}"
            )
            self.assertEqual(part_body, _DATA[100:200])
            self.assertEqual(session.failures, [])
        self.assertEqual(source.streams, [(0, len(_DATA)), (100, 100)])

    async def test_head_does_not_read_the_source(self) -> None:
        # given
        source = _Source()
        async with self.service.session(source) as session:
            # when
            async with self.http.head(session.url) as resp:
                pass

            # then
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Length"], str(len(_DATA)))
            self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
        self.assertEqual(source.streams, [])

    async def test_sessions_are_separate(self) -> None:
        # given
        async with (
            self.service.session(_Source(b"first")) as one,
            self.service.session(_Source(b"second")) as two,
        ):
            # when
            async with self.http.get(one.url) as a, self.http.get(two.url) as b:
                bodies = (await a.read(), await b.read())

        # then
        self.assertNotEqual(one.url, two.url)
        self.assertEqual(bodies, (b"first", b"second"))

    async def test_ended_session_is_404(self) -> None:
        # given
        async with self.service.session(_Source()) as session:
            url = session.url

        # when
        async with self.http.get(url) as resp:
            # then
            self.assertEqual(resp.status, 404)

    async def test_unknown_token_is_404(self) -> None:
        async with self.service.session(_Source()) as session:
            # when
            async with self.http.get(session.url + "x") as resp:
                # then
                self.assertEqual(resp.status, 404)

    async def test_source_failure_is_recorded_and_aborts(self) -> None:
        with self.assertLogs("aiohttp.server", "ERROR"):
            async with self.service.session(_Source(fail_after=8192)) as session:
                # when
                async with self.http.get(session.url) as resp:
                    with self.assertRaises(ClientPayloadError):
                        await resp.read()

                # then
                self.assertEqual(len(session.failures), 1)
                self.assertIn("connection reset", session.failures[0])
                self.assertFalse(session.timed_out)

    async def test_short_stream_is_recorded(self) -> None:
        with self.assertLogs("aiohttp.server", "ERROR"):
            async with self.service.session(_Source(stop_after=8192)) as session:
                # when
                async with self.http.get(session.url) as resp:
                    with self.assertRaises(ClientPayloadError):
                        await resp.read()

                # then
                self.assertEqual(len(session.failures), 1)
                self.assertIn("ended after 8192", session.failures[0])

    async def test_stalled_read_times_out_and_is_recorded(self) -> None:
        # given
        source = _Source(stall_after=4096)
        with self.assertLogs("aiohttp.server", "ERROR"):
            async with self.service.session(source) as session:
                async with self.http.get(session.url) as resp:
                    await source.stalled.wait()
                    self.assertTrue(session.reading)

                    # when
                    with self.assertRaises(ClientPayloadError):
                        await resp.read()

                # then
                self.assertTrue(session.timed_out)
                self.assertFalse(session.reading)
                self.assertEqual(len(session.failures), 1)

    async def test_client_closing_early_is_not_a_failure(self) -> None:
        # given: MediaInfo closes a response early to seek elsewhere
        source = _Source(_DATA * 64)
        async with self.service.session(source) as session:
            async with self.http.get(session.url) as resp:
                await resp.content.readexactly(10)
                # when
                resp.close()
            for _ in range(500):
                if not source.open_streams:
                    break
                await asyncio.sleep(0.01)

            # then
            self.assertEqual(source.open_streams, 0)
            self.assertEqual(session.failures, [])

    async def test_ending_the_session_cancels_reads_in_flight(self) -> None:
        # given
        source = _Source(stall_after=0)
        async with self.service.session(source) as session:
            request = asyncio.ensure_future(self.http.get(session.url))
            await source.stalled.wait()
            # when: the block ends while a read is in flight

        # then: the read is gone before the caller could close the source
        self.assertEqual(source.open_streams, 0)
        self.assertFalse(session.reading)
        self.assertEqual(session.failures, [])
        try:
            resp = await request
            with self.assertRaises(ClientPayloadError):
                await resp.read()
        except Exception:
            pass  # the connection may be dropped before the headers arrive

    async def test_close_revokes_every_session(self) -> None:
        # given
        source = _Source(stall_after=0)
        async with self.service.session(source) as session:
            request = asyncio.ensure_future(self.http.get(session.url))
            await source.stalled.wait()

            # when
            await self.service.close()

            # then
            self.assertEqual(source.open_streams, 0)
        request.cancel()
        (resp,) = await asyncio.gather(request, return_exceptions=True)
        if not isinstance(resp, BaseException):
            resp.close()

    async def test_session_needs_a_running_service(self) -> None:
        # given
        await self.service.close()

        # when / then
        with self.assertRaises(RuntimeError):
            async with self.service.session(_Source()):
                pass

    async def test_other_errors_are_recorded_by_type_only(self) -> None:
        # given: an upstream error whose text carries a session ID
        source = _Source(fail_after=4096, fail_with=OSError("GET ?_sid=secret"))
        with self.assertLogs("aiohttp.server", "ERROR"):
            async with self.service.session(source) as session:
                # when
                async with self.http.get(session.url) as resp:
                    with self.assertRaises(ClientPayloadError):
                        await resp.read()

                # then
                self.assertEqual(len(session.failures), 1)
                self.assertIn("OSError", session.failures[0])
                self.assertNotIn("secret", session.failures[0])

    async def test_closing_during_a_stalled_read_stops_it_at_once(self) -> None:
        # given: a read that would only time out much later
        service = await self.enterAsyncContext(ProbeSourceService(read_timeout=30))
        source = _Source(stall_after=4096)
        async with service.session(source) as session:
            async with self.http.get(session.url) as resp:
                await resp.content.readexactly(4096)
                await source.stalled.wait()
                # when: MediaInfo closes the response to seek elsewhere
                resp.close()
            async with asyncio.timeout(5):
                while source.open_streams:
                    await asyncio.sleep(0.01)

            # then
            self.assertEqual(session.failures, [])
            self.assertFalse(session.reading)

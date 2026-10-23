"""Serve open media sources to local MediaInfo processes over HTTP.

MediaInfo reads a URL with its own Range requests; this service answers them
from the ``MediaSource`` a backend opened for one probe. It listens on loopback
only, on an ephemeral port, and is never a route of the main app: the MediaInfo
subprocess shares the server's network namespace. A URL names a source by an
opaque token valid for one probe; the service never takes paths or upstream
URLs.

MediaInfo can return dimensions after an interrupted transfer, so the probe
session records every read the source could not complete, and the probe decides
from that record, not from MediaInfo's output alone.
"""

import asyncio
import secrets
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from logging import getLogger

from aiohttp import web

from ...._lib.ranges import serve_range
from .source import MediaSource, MediaSourceError


_L = getLogger(__name__)

_HOST = "127.0.0.1"


class ProbeSession:
    """One probe's view of the service: its URL and what went wrong serving it."""

    def __init__(self, source: MediaSource, url: str, *, read_timeout: float) -> None:
        self.source = source
        self.url = url
        self._read_timeout = read_timeout
        # Reads the source could not complete, timeouts among them.
        self.failures: list[str] = []
        self.timed_out = False
        self._reads_in_flight = 0
        self.requests: set[asyncio.Task[object]] = set()

    @property
    def reading(self) -> bool:
        """Whether a backend read is in flight now."""
        return self._reads_in_flight > 0

    def record_failure(self, reason: str, cause: Exception | None) -> None:
        # A source's own errors are worded to be logged; anything else may
        # carry an upstream URL, so only its type is kept.
        if isinstance(cause, MediaSourceError):
            reason = f"{reason}: {cause}"
        self.failures.append(reason)

    def open_range(
        self, offset: int, length: int
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        return aclosing(self._stream(offset, length))

    async def _stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async with aclosing(self.source.stream(offset, length)) as chunks:
            while True:
                self._reads_in_flight += 1
                try:
                    async with asyncio.timeout(self._read_timeout):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    return
                except TimeoutError as e:
                    self.timed_out = True
                    raise MediaSourceError(
                        f"reading at {offset} took more than {self._read_timeout}s"
                    ) from e
                finally:
                    self._reads_in_flight -= 1
                yield chunk


class ProbeSourceService:
    """A loopback HTTP listener serving the sources of running probes.

    Use as an async context manager; start it before any probe.
    """

    def __init__(self, *, read_timeout: float = 30.0) -> None:
        self._read_timeout = read_timeout
        self._sessions: dict[str, ProbeSession] = {}
        self._runner: web.AppRunner | None = None
        self._base_url = ""

    async def __aenter__(self) -> "ProbeSourceService":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{token}", self._handle)
        # No access log: the path is the token. MediaInfo closing a response to
        # seek cancels its handler, which stops the upstream read at once
        # instead of when its next chunk arrives or times out.
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, _HOST, 0)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        port = runner.addresses[0][1]
        self._base_url = f"http://{_HOST}:{port}"

    async def close(self) -> None:
        """Revoke every session and stop listening."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(_finish(s) for s in sessions))
        if self._runner is not None:
            runner, self._runner = self._runner, None
            await runner.cleanup()

    @asynccontextmanager
    async def session(self, source: MediaSource) -> AsyncGenerator[ProbeSession]:
        """Serve *source* until the block ends.

        Leaving the block revokes the token and cancels requests still being
        served, so the caller may close the source afterwards.
        """
        if self._runner is None:
            raise RuntimeError("the probe source service is not running")
        token = secrets.token_urlsafe(24)
        session = ProbeSession(
            source, f"{self._base_url}/{token}", read_timeout=self._read_timeout
        )
        self._sessions[token] = session
        try:
            yield session
        finally:
            self._sessions.pop(token, None)
            await _finish(session)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        session = self._sessions.get(request.match_info["token"])
        if session is None:
            raise web.HTTPNotFound()
        task = asyncio.current_task()
        assert task is not None
        session.requests.add(task)
        try:
            return await serve_range(
                request,
                size=session.source.size,
                content_type="application/octet-stream",
                open_range=session.open_range,
                on_source_failure=session.record_failure,
            )
        finally:
            session.requests.discard(task)


async def _finish(session: ProbeSession) -> None:
    """Cancel requests still being served for *session* and wait for them."""
    tasks = list(session.requests)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

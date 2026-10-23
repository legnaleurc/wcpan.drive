"""ReadableFile that downloads via the wcpan.drive.server feed server."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, override

from aiohttp import ClientResponse, ClientSession
from wcpan.drive.client.types import Node, ReadableFile
from wcpan.drive.protocol.lib import parse_content_range

from ._lib import node_url
from .exceptions import SynologyServerError


class ClientReadableFile(ReadableFile):
    def __init__(
        self,
        node: Node,
        *,
        session: ClientSession,
        server_url: str,
    ) -> None:
        self._session = session
        self._server_url = server_url.rstrip("/")
        self._node = node
        self._offset = 0
        self._response: ClientResponse | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> ReadableFile:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._close()

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._at_end():
            return
        await self._open()
        assert self._response
        async for chunk in self._response.content.iter_any():
            yield chunk

    @override
    async def read(self, length: int) -> bytes:
        if self._at_end():
            return b""
        await self._open()
        assert self._response
        chunk = await self._response.content.read(length)
        self._offset += len(chunk)
        return chunk

    @override
    async def seek(self, offset: int) -> int:
        if self._node.size <= 0:
            return 0
        self._offset = offset
        await self._close()
        # At or past the end there is no range to request; reads return b"".
        if not self._at_end():
            await self._open()
        return self._offset

    @override
    async def node(self) -> Node:
        return self._node

    def _at_end(self) -> bool:
        return self._offset >= self._node.size

    @asynccontextmanager
    async def _download(self) -> AsyncGenerator[ClientResponse]:
        url = node_url(self._server_url, self._node.id, "download")
        headers: dict[str, str] = {}
        last = self._node.size - 1
        if self._offset > 0:
            headers["Range"] = f"bytes={self._offset}-{last}"
        async with self._session.get(url, headers=headers) as response:
            response.raise_for_status()
            if self._offset > 0:
                _check_range(response, first=self._offset, last=last)
            yield response

    async def _open(self) -> None:
        if not self._response:
            async with AsyncExitStack() as stack:
                self._response = await stack.enter_async_context(self._download())
                self._stack = stack.pop_all()

    async def _close(self) -> None:
        if self._response:
            assert self._stack
            await self._stack.aclose()
            self._response = None
            self._stack = None


def _check_range(response: ClientResponse, *, first: int, last: int) -> None:
    """Fail unless *response* holds exactly bytes *first* to *last*.

    A server that ignores the range answers 200 with the whole file, which
    would be read as if it started at *first*. The total may differ from the
    node's size if the file changed since; the positions are what was asked for.
    """
    content_range = response.headers.get("Content-Range", "")
    parsed = parse_content_range(content_range)
    if response.status != 206 or parsed is None or parsed[:2] != (first, last):
        raise SynologyServerError(
            f"expected 206 for bytes {first}-{last}, got {response.status} "
            f"with Content-Range {content_range!r}",
            status=response.status,
        )

"""ReadableFile that downloads via the wcpan.drive.server feed server."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, override

from aiohttp import ClientResponse, ClientSession
from wcpan.drive.client.types import Node, ReadableFile


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
        if self._node.size <= 0:
            return
        await self._open()
        assert self._response
        async for chunk in self._response.content.iter_any():
            yield chunk

    @override
    async def read(self, length: int) -> bytes:
        if self._node.size <= 0:
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
        await self._open()
        return self._offset

    @override
    async def node(self) -> Node:
        return self._node

    @asynccontextmanager
    async def _download(self) -> AsyncGenerator[ClientResponse]:
        url = f"{self._server_url}/api/v1/nodes/{self._node.id}/download"
        headers: dict[str, str] = {}
        if self._offset > 0:
            end = self._node.size - 1
            headers["Range"] = f"bytes={self._offset}-{end}"
        async with self._session.get(url, headers=headers) as response:
            response.raise_for_status()
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

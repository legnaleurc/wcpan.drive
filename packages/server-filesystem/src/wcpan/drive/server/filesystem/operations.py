"""Read-only node operations and uploads for the filesystem backend."""

from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from typing import BinaryIO, NoReturn

from wcpan.drive.protocol.types import MediaInfo, MirrorStableId, NodeRecord
from wcpan.drive.server.services.operations import (
    OperationNotSupportedError,
    OperationUnavailableError,
)

from .blocking import Blocking
from .paths import NodePaths
from .reading import open_verified
from .store import FilesystemStore


_CHUNK_SIZE = 1024 * 1024
_READ_ONLY = "The filesystem backend is read-only"


def _read_only() -> NoReturn:
    raise OperationNotSupportedError(_READ_ONLY)


class FilesystemOperations:
    """Serves downloads by path; every mutation is rejected."""

    def __init__(
        self, *, paths: NodePaths, store: FilesystemStore, blocking: Blocking
    ) -> None:
        self._paths = paths
        self._store = store
        self._blocking = blocking

    async def create_folder(self, parent_id: MirrorStableId, name: str) -> NodeRecord:
        _read_only()

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: MirrorStableId | None,
    ) -> NodeRecord:
        _read_only()

    async def delete(self, record: NodeRecord) -> None:
        _read_only()

    @asynccontextmanager
    async def download(
        self, record: NodeRecord, *, range_: slice | None
    ) -> AsyncGenerator[AsyncIterable[bytes], None]:
        path = await self._paths.path_of(record.id)
        state = (await self._store.get_entries([record.id])).get(record.id)
        if path is None or state is None:
            raise OperationUnavailableError(f"{record.id} has no local path")
        try:
            f = await self._blocking(open_verified, path, state.identity)
        except OSError as e:
            raise OperationUnavailableError(str(e)) from e
        try:
            yield self._chunks(f, range_)
        finally:
            await self._blocking(f.close)

    async def _chunks(self, f: BinaryIO, range_: slice | None) -> AsyncIterator[bytes]:
        size = await self._blocking(lambda: f.seek(0, 2))
        start, stop, _ = (range_ or slice(None)).indices(size)
        await self._blocking(f.seek, start)
        remaining = max(stop - start, 0)
        while remaining > 0:
            chunk = await self._blocking(f.read, min(_CHUNK_SIZE, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


class FilesystemUploader:
    """Uploads are not supported by the read-only backend."""

    async def find_conflict(
        self, parent_id: MirrorStableId, name: str
    ) -> NodeRecord | None:
        _read_only()

    async def upload(
        self,
        parent_id: MirrorStableId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        _read_only()

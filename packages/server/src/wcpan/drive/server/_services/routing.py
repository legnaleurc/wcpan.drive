"""Send each node operation and upload to the backend serving its mount."""

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord

from .mirror.store import (
    MirrorStore,
)
from .mounts import Mounts
from .operations import (
    NodeOperations,
    OperationConflictError,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from .upload.service import FileOpener, UploadConflictError, Uploader, WritableFile


type Barrier = Callable[[], Awaitable[None]]
"""Wait until every mirror write queued so far has landed."""


class BackendRouter:
    """``NodeOperations``, ``Uploader``, and ``FileOpener`` over several backends.

    A mutation returns only after *barrier*, so its mirror writes have landed
    and the next request sees them (server SPEC, Mutation visibility).
    """

    def __init__(
        self,
        *,
        mirror: MirrorStore,
        mounts: Mounts,
        mount_backends: Mapping[str, str],
        operations: Mapping[str, NodeOperations],
        uploaders: Mapping[str, Uploader],
        files: Mapping[str, FileOpener],
        barrier: Barrier,
    ) -> None:
        self._mirror = mirror
        self._mounts = mounts
        self._mount_backends = mount_backends
        self._operations = operations
        self._uploaders = uploaders
        self._files = files
        self._barrier = barrier

    async def backend_of(self, node_id: NodeId) -> str:
        """The backend instance serving *node_id*.

        A mount is served by its configured backend, as create, upload, and move
        destinations usually are mounts; any other node by the instance whose
        ID map row it has, which also covers nodes not published yet.
        """
        if node_id == self._mounts.root_id:
            raise OperationNotSupportedError("Cannot change the server root")
        mount = self._mounts.by_id(node_id)
        if mount is not None:
            backend = self._mount_backends.get(mount.name)
        else:
            backend = await self._mirror.owner_of(node_id)
        if backend is None:
            raise OperationUnavailableError(f"{node_id} is not served by a backend")
        if backend not in self._operations:
            raise OperationUnavailableError(f"backend {backend!r} is not running")
        return backend

    async def create_folder(self, parent_id: NodeId, name: str) -> NodeRecord:
        backend = await self.backend_of(parent_id)
        return await _visible(
            self._operations[backend].create_folder(parent_id, name), self._barrier
        )

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: NodeId | None,
    ) -> NodeRecord:
        backend = await self.backend_of(record.id)
        if parent_id is not None and parent_id != record.parent_id:
            if await self.backend_of(parent_id) != backend:
                raise OperationNotSupportedError("Cannot move between backends")
        return await _visible(
            self._operations[backend].update(record, name=name, parent_id=parent_id),
            self._barrier,
        )

    async def delete(self, record: NodeRecord) -> None:
        backend = await self.backend_of(record.id)
        await _visible(self._operations[backend].delete(record), self._barrier)

    def download(
        self, record: NodeRecord, *, offset: int, length: int
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        return _RoutedDownload(self, record, offset, length)

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        backend = await self.backend_of(parent_id)
        return await self._uploaders[backend].find_conflict(parent_id, name)

    async def upload(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        backend = await self.backend_of(parent_id)
        return await _visible(
            self._uploaders[backend].upload(
                parent_id, name, content, mime_type=mime_type, media_info=media_info
            ),
            self._barrier,
        )

    async def open(
        self,
        parent_id: NodeId,
        name: str,
        size: int,
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> WritableFile:
        # The file stays bound to this backend for the rest of the session.
        backend = await self.backend_of(parent_id)
        file = await self._files[backend].open(
            parent_id, name, size, mime_type=mime_type, media_info=media_info
        )
        return _VisibleFile(file, self._barrier)

    def operations_of(self, backend: str) -> NodeOperations:
        return self._operations[backend]


async def _visible[R](mutation: Awaitable[R], barrier: Barrier) -> R:
    """The result of *mutation*, returned once its mirror writes have landed.

    A conflict also waits: its record may have just been observed, and the
    client can use it right away.
    """
    try:
        result = await mutation
    except (OperationConflictError, UploadConflictError):
        await barrier()
        raise
    await barrier()
    return result


class _VisibleFile(WritableFile):
    """A backend file whose ``close`` returns once the published node is mirrored."""

    def __init__(self, file: WritableFile, barrier: Barrier) -> None:
        self._file = file
        self._barrier = barrier

    async def write(self, offset: int, data: bytes) -> int:
        return await self._file.write(offset, data)

    async def close(self) -> NodeRecord:
        return await _visible(self._file.close(), self._barrier)

    async def abort(self) -> None:
        await self._file.abort()


class _RoutedDownload(AbstractAsyncContextManager[AsyncIterable[bytes]]):
    """Resolve the backend when entered, since ``download`` itself is not async."""

    def __init__(
        self, router: BackendRouter, record: NodeRecord, offset: int, length: int
    ) -> None:
        self._router = router
        self._record = record
        self._offset = offset
        self._length = length
        self._inner: AbstractAsyncContextManager[AsyncIterable[bytes]] | None = None

    async def __aenter__(self) -> AsyncIterable[bytes]:
        backend = await self._router.backend_of(self._record.id)
        self._inner = self._router.operations_of(backend).download(
            self._record, offset=self._offset, length=self._length
        )
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc_info: object) -> bool | None:
        assert self._inner is not None
        return await self._inner.__aexit__(*exc_info)  # pyright: ignore[reportArgumentType]

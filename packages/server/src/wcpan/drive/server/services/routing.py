"""Send each node operation and upload to the backend serving its mount."""

from collections.abc import AsyncIterable, Mapping
from contextlib import AbstractAsyncContextManager

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord

from ..lib.mounts import SERVER_ROOT_ID, mount_name
from .operations import (
    NodeOperations,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from .storage import StorageService
from .upload import Uploader


class BackendRouter:
    """``NodeOperations`` and ``Uploader`` over several backend instances."""

    def __init__(
        self,
        *,
        storage: StorageService,
        mount_backends: Mapping[str, str],
        operations: Mapping[str, NodeOperations],
        uploaders: Mapping[str, Uploader],
    ) -> None:
        self._storage = storage
        self._mount_backends = mount_backends
        self._operations = operations
        self._uploaders = uploaders

    async def backend_of(self, node_id: NodeId) -> str:
        """The backend instance serving the mount that contains *node_id*."""
        if node_id == SERVER_ROOT_ID:
            raise OperationNotSupportedError("Cannot change the server root")
        name = mount_name(node_id)
        if name is None:
            ancestors = await self._storage.get_ancestors(node_id)
            top = ancestors[-1] if ancestors else None
            if top is not None and top.parent_id is not None:
                name = mount_name(top.parent_id)
        backend = self._mount_backends.get(name) if name is not None else None
        if backend is None:
            raise OperationUnavailableError(f"{node_id} is not in a configured mount")
        return backend

    async def create_folder(self, parent_id: NodeId, name: str) -> NodeRecord:
        backend = await self.backend_of(parent_id)
        return await self._operations[backend].create_folder(parent_id, name)

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
        return await self._operations[backend].update(
            record, name=name, parent_id=parent_id
        )

    async def delete(self, record: NodeRecord) -> None:
        backend = await self.backend_of(record.id)
        await self._operations[backend].delete(record)

    def download(
        self, record: NodeRecord, *, range_: slice | None
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        return _RoutedDownload(self, record, range_)

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
        return await self._uploaders[backend].upload(
            parent_id, name, content, mime_type=mime_type, media_info=media_info
        )

    def operations_of(self, backend: str) -> NodeOperations:
        return self._operations[backend]


class _RoutedDownload(AbstractAsyncContextManager[AsyncIterable[bytes]]):
    """Resolve the backend when entered, since ``download`` itself is not async."""

    def __init__(
        self, router: BackendRouter, record: NodeRecord, range_: slice | None
    ) -> None:
        self._router = router
        self._record = record
        self._range = range_
        self._inner: AbstractAsyncContextManager[AsyncIterable[bytes]] | None = None

    async def __aenter__(self) -> AsyncIterable[bytes]:
        backend = await self._router.backend_of(self._record.id)
        self._inner = self._router.operations_of(backend).download(
            self._record, range_=self._range
        )
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc_info: object) -> bool | None:
        assert self._inner is not None
        return await self._inner.__aexit__(*exc_info)  # pyright: ignore[reportArgumentType]

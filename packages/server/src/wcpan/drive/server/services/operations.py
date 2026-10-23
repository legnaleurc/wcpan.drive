"""Backend-neutral node operations used by the HTTP handlers."""

from collections.abc import AsyncIterable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord


class OperationConflictError(Exception):
    """The target name is already taken.

    ``record`` is the existing node when the backend could resolve it.
    """

    def __init__(self, message: str, *, record: NodeRecord | None = None) -> None:
        super().__init__(message)
        self.record = record


class OperationUnavailableError(Exception):
    """The backend failed or returned an unusable result; a retry may succeed."""


class NodeOperations(Protocol):
    """Mutate and read backend nodes, keeping the mirror in step.

    Implementations order their mirror writes through ``NodeSyncService`` and
    translate backend errors into the exceptions above.
    """

    async def create_folder(self, parent_id: MirrorStableId, name: str) -> NodeRecord:
        """Create a directory and return its mirrored record."""
        ...

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: MirrorStableId | None,
    ) -> NodeRecord:
        """Rename and/or move *record*; ``None`` leaves that field unchanged."""
        ...

    async def delete(self, record: NodeRecord) -> None:
        """Delete *record* and its subtree."""
        ...

    def download(
        self, record: NodeRecord, *, range_: slice | None
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        """Stream the content of *record*.

        A client disconnect while consuming the stream surfaces as
        ``ConnectionError``; other stream failures raise
        ``OperationUnavailableError``.
        """
        ...

"""Backend-neutral node operations used by the HTTP handlers."""

from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..types import NodeObservation


class OperationConflictError(Exception):
    """The target name is already taken.

    ``record`` is the existing node when the backend could resolve it.
    """

    def __init__(self, message: str, *, record: NodeRecord | None = None) -> None:
        super().__init__(message)
        self.record = record


class OperationUnavailableError(Exception):
    """The backend failed or returned an unusable result; a retry may succeed."""


class OperationNotSupportedError(Exception):
    """The backend does not support this operation, for example because it is read-only."""


class OperationInvalidError(Exception):
    """The request cannot be applied as given, for example an invalid name."""


class LocateUndecidedError(Exception):
    """The backend could not tell whether a node still exists."""


# Fetch a node directly from the backend. Returns ``None`` when the node no longer
# exists under a mirrored parent; raises ``LocateUndecidedError`` when the backend
# cannot answer.
type Locate = Callable[[NodeId], Awaitable[NodeObservation | None]]


class NodeOperations(Protocol):
    """Mutate and read backend nodes, keeping the mirror in step.

    Implementations order their mirror writes through ``NodeSyncService`` and
    translate backend errors into the exceptions above.
    """

    async def create_folder(self, parent_id: NodeId, name: str) -> NodeRecord:
        """Create a directory and return its mirrored record."""
        ...

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: NodeId | None,
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

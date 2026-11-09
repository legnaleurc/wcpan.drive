"""Upload workflow service plus in-memory resumable session store.

A resumable session is a ``WritableFile`` that the core opened on the parent's
backend. The backend decides how to store it: in place where it can, or in a
local spool (``spool.py``) for backends that take a file only whole. The core
owns the request stream and counts only the bytes the file reports kept.
"""

import asyncio
import secrets
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from enum import Enum, auto
from logging import getLogger
from typing import Protocol

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord

from ..lib.names import normalize_name


_L = getLogger(__name__)

FILE_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB per file write
UPLOAD_SESSION_TTL = 60 * 60
UPLOAD_CLEANUP_INTERVAL = 5 * 60


class UploadSessionNotFoundError(Exception):
    pass


class UploadInvalidChunkError(Exception):
    pass


class UploadBusyError(Exception):
    """The session is publishing its upload and can no longer be cancelled."""


class UploadTransientError(Exception):
    pass


class UploadPermanentError(Exception):
    pass


class UploadNameTooLongError(UploadPermanentError):
    def __init__(self, name: str) -> None:
        super().__init__(f"File name is too long for the backend: {name!r}")
        self.name = name


class UploadParentGoneError(UploadPermanentError):
    """The parent folder given when the upload began no longer exists."""


class UploadConflictError(Exception):
    def __init__(self, record: NodeRecord) -> None:
        super().__init__()
        self.record = record


class UploadOffsetMismatchError(Exception):
    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset


class Uploader(Protocol):
    """Backend half of an upload: conflict lookup and the transfer itself."""

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        """The existing node named *name* under *parent_id*, if any."""
        ...

    async def upload(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        """Upload *content* and mirror the new node.

        Raises ``UploadConflictError``, ``UploadTransientError``,
        ``UploadNameTooLongError``, or ``UploadPermanentError``.
        """
        ...


class WritableFile(Protocol):
    """A file opened for one resumable upload, with random access.

    The core calls one method at a time, never seeks past the bytes it saw
    kept, and calls nothing after a successful ``close`` or any ``abort``.
    """

    async def seek(self, offset: int) -> None:
        """Move the position to *offset*, which the core has seen kept."""
        ...

    async def write(self, data: bytes) -> int:
        """Write *data* at the position and return how many bytes were kept.

        Kept bytes are stored and will not be lost while the file stays open in
        this process; they need not be durable yet. Returns 1 to ``len(data)``
        and advances the position by that much.

        A failed or cancelled write may have stored part of *data*; the core
        counts it as nothing kept, seeks back, and writes that range again,
        possibly with different bytes.
        Raises ``UploadTransientError`` or ``UploadPermanentError``.
        """
        ...

    async def flush(self) -> None:
        """Push buffered bytes to the backend's storage; never publishes."""
        ...

    async def close(self) -> NodeRecord:
        """Publish the written bytes under the parent and name given to ``open``.

        Never replaces an existing entry, and never publishes under a different
        folder than that parent. Unlike ``io`` close, a failure other than a
        conflict, including cancellation, leaves the file open: the core may
        close again, and a retry after a partial publish finishes it rather than
        publishing twice.

        Raises ``UploadConflictError``, ``UploadTransientError``,
        ``UploadNameTooLongError``, ``UploadParentGoneError``, or
        ``UploadPermanentError``.
        """
        ...

    async def abort(self) -> None:
        """Close without publishing; safe to call more than once.

        Never removes an entry that ``close`` already published.
        """
        ...


class FileOpener(Protocol):
    """Opens files for resumable uploads on a backend."""

    async def open(
        self,
        parent_id: NodeId,
        name: str,
        size: int,
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> WritableFile:
        """A new, empty file for *name* under *parent_id*, of *size* bytes when done.

        Releases anything it allocated if it fails or is cancelled.
        """
        ...


def _idle_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


class SessionState(Enum):
    IDLE = auto()
    WRITING = auto()
    COMMITTING = auto()


@dataclass
class UploadSession:
    session_id: str
    parent_id: NodeId
    name: str
    total_size: int
    mime_type: str | None
    media_info: MediaInfo | None
    file: WritableFile
    received: int = 0
    state: SessionState = SessionState.IDLE
    # Set when the session is removed while writing; the writer then aborts.
    cancelled: bool = False
    published: bool = False
    last_activity: float = 0.0
    # Set while no request is using the file.
    idle: asyncio.Event = field(default_factory=_idle_event)


class UploadSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, UploadSession] = {}

    def add(self, session: UploadSession) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> UploadSession | None:
        return self._sessions.get(session_id)

    def pop(self, session_id: str) -> UploadSession | None:
        return self._sessions.pop(session_id, None)

    def pop_expired(self, now: float, ttl: float) -> list[UploadSession]:
        """Remove idle sessions without activity for *ttl* seconds."""
        expired = [
            session
            for session in self._sessions.values()
            if session.state is SessionState.IDLE and now - session.last_activity >= ttl
        ]
        for session in expired:
            del self._sessions[session.session_id]
        return expired

    def pop_all(self) -> list[UploadSession]:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        return sessions


@dataclass
class SessionStatus:
    received: int
    total_size: int


@dataclass
class UploadCreated:
    session_id: str
    total_size: int


@dataclass
class UploadAccepted:
    offset: int


@dataclass
class UploadCreatedNode:
    record: NodeRecord


class UploadService:
    def __init__(
        self,
        *,
        store: UploadSessionStore,
        uploader: Uploader,
        files: FileOpener,
    ) -> None:
        self._store = store
        self._uploader = uploader
        self._files = files

    async def create_session(
        self,
        parent_id: NodeId,
        name: str,
        total_size: int,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> UploadCreated:
        name = normalize_name(name)
        conflict = await self._uploader.find_conflict(parent_id, name)
        if conflict is not None:
            raise UploadConflictError(conflict)
        file = await self._files.open(
            parent_id, name, total_size, mime_type=mime_type, media_info=media_info
        )
        # No await between open and registration, so the file cannot leak.
        session = UploadSession(
            session_id=secrets.token_urlsafe(32),
            parent_id=parent_id,
            name=name,
            total_size=total_size,
            mime_type=mime_type,
            media_info=media_info,
            file=file,
            last_activity=time.monotonic(),
        )
        self._store.add(session)
        return UploadCreated(session_id=session.session_id, total_size=total_size)

    def get_session_status(self, session_id: str) -> SessionStatus | None:
        session = self._store.get(session_id)
        if session is None:
            return None
        session.last_activity = time.monotonic()
        return SessionStatus(received=session.received, total_size=session.total_size)

    async def delete_session(self, session_id: str) -> bool:
        """Cancel *session_id*; ``False`` if there is no such session.

        Raises ``UploadBusyError`` once the upload is being published.
        """
        session = self._store.get(session_id)
        if session is None:
            return False
        if session.state is SessionState.COMMITTING:
            raise UploadBusyError()
        self._store.pop(session_id)
        if session.state is SessionState.WRITING:
            # The writer checks this before every write and before committing.
            session.cancelled = True
        else:
            await self._abort(session)
        return True

    async def remove_expired(self, now: float, ttl: float) -> int:
        expired = self._store.pop_expired(now, ttl)
        for session in expired:
            await self._abort(session)
        return len(expired)

    async def cleanup_expired_sessions(self) -> None:
        while True:
            await asyncio.sleep(UPLOAD_CLEANUP_INTERVAL)
            removed = await self.remove_expired(time.monotonic(), UPLOAD_SESSION_TTL)
            if removed:
                _L.info("removed %d expired upload session(s)", removed)

    async def close(self) -> None:
        """Abort every session and wait for active requests to leave its file.

        Run before the backends close, once no new requests arrive.
        """
        for session in self._store.pop_all():
            writing = session.state is SessionState.WRITING
            if writing:
                # The writer aborts once it sees this.
                session.cancelled = True
            await session.idle.wait()
            if not writing and not session.published:
                await self._abort(session)

    async def upload_direct(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> UploadCreatedNode:
        name = normalize_name(name)
        conflict = await self._uploader.find_conflict(parent_id, name)
        if conflict is not None:
            raise UploadConflictError(conflict)
        record = await self._uploader.upload(
            parent_id,
            name,
            content,
            mime_type=mime_type,
            media_info=media_info,
        )
        return UploadCreatedNode(record=record)

    async def append_chunk(
        self,
        session_id: str,
        start: int,
        content: AsyncIterable[bytes],
    ) -> UploadAccepted | UploadCreatedNode:
        session = self._store.get(session_id)
        if session is None:
            raise UploadSessionNotFoundError()

        if session.state is not SessionState.IDLE or start != session.received:
            raise UploadOffsetMismatchError(session.received)

        session.state = SessionState.WRITING
        session.idle.clear()
        session.last_activity = time.monotonic()
        try:
            # A failed request may have left the position anywhere.
            await session.file.seek(session.received)
            await self._receive(session, content)
            if session.cancelled:
                raise UploadSessionNotFoundError()
            if session.received != session.total_size:
                return UploadAccepted(offset=session.received)
            # Checked and set without an await in between, so a DELETE either
            # cancelled the writes above or finds the session committing.
            session.state = SessionState.COMMITTING
            return await self._finalize_session(session)
        finally:
            session.state = SessionState.IDLE
            try:
                if session.cancelled:
                    await self._abort(session)
            finally:
                session.idle.set()

    async def _receive(
        self, session: UploadSession, content: AsyncIterable[bytes]
    ) -> None:
        buffer = bytearray()
        async for chunk in content:
            if session.cancelled:
                raise UploadSessionNotFoundError()
            if session.received + len(buffer) + len(chunk) > session.total_size:
                raise UploadInvalidChunkError("Upload-Offset exceeds Upload-Length")
            buffer += chunk
            while len(buffer) >= FILE_CHUNK_SIZE:
                await self._write(session, bytes(buffer[:FILE_CHUNK_SIZE]))
                del buffer[:FILE_CHUNK_SIZE]
        if buffer and not session.cancelled:
            await self._write(session, bytes(buffer))

    async def _write(self, session: UploadSession, data: bytes) -> None:
        # Immutable batches: a write still running after a cancellation can
        # never see the buffer change under it.
        while data:
            kept = await session.file.write(data)
            if not 0 < kept <= len(data):
                raise UploadTransientError(f"Backend kept {kept} of {len(data)} bytes")
            session.received += kept
            session.last_activity = time.monotonic()
            data = data[kept:]

    async def _finalize_session(self, session: UploadSession) -> UploadCreatedNode:
        _L.debug(
            "begin finalising upload session session_id=%r name=%r parent_id=%r",
            session.session_id,
            session.name,
            session.parent_id,
        )
        try:
            record = await session.file.close()
        except UploadConflictError:
            _L.debug(
                "upload session conflicted during finalisation session_id=%r name=%r parent_id=%r",
                session.session_id,
                session.name,
                session.parent_id,
            )
            self._store.pop(session.session_id)
            await self._abort(session)
            raise
        except UploadTransientError as e:
            _L.warning(
                "upload session failed during finalisation session_id=%r name=%r parent_id=%r error=%s",
                session.session_id,
                session.name,
                session.parent_id,
                e,
            )
            raise
        except UploadNameTooLongError:
            raise
        except UploadPermanentError as e:
            _L.warning(
                "upload session permanently failed during finalisation session_id=%r name=%r parent_id=%r error=%s",
                session.session_id,
                session.name,
                session.parent_id,
                e,
            )
            raise
        session.published = True
        self._store.pop(session.session_id)

        _L.debug(
            "finalised upload session session_id=%r name=%r parent_id=%r id=%r",
            session.session_id,
            session.name,
            session.parent_id,
            record.id,
        )
        return UploadCreatedNode(record=record)

    async def _abort(self, session: UploadSession) -> None:
        try:
            await session.file.abort()
        except Exception:
            _L.warning(
                "failed to discard upload session session_id=%r name=%r",
                session.session_id,
                session.name,
                exc_info=True,
            )

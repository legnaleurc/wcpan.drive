"""Upload workflow service plus in-memory resumable session store.

A resumable session writes into an ``UploadStage`` that its backend owns: native
staging where the backend has it, otherwise a local spool (``spool.py``). The
core owns the request stream, so it always knows exactly how many bytes the
stage accepted.
"""

import asyncio
import secrets
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass
from enum import Enum, auto
from logging import getLogger
from typing import Protocol

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord

from ..lib.names import normalize_name


_L = getLogger(__name__)

FILE_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB per stage write
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


class UploadStage(Protocol):
    """Storage for one resumable upload, published by ``commit``.

    The core calls one method at a time, and nothing after a successful
    ``commit`` or any ``abort``.
    """

    async def write_at(self, offset: int, data: bytes) -> None:
        """Store all of *data* at *offset*, or raise.

        After a failure or cancellation the core retries from the last offset
        it saw accepted, so a range may be written again with the same bytes.
        Raises ``UploadTransientError`` or ``UploadPermanentError``.
        """
        ...

    async def commit(self) -> NodeRecord:
        """Publish the staged bytes under the parent and name given to ``begin``.

        Never replaces an existing entry, and never publishes under a different
        folder than that parent. After any error but a conflict the stage is
        still usable, so the core may commit again or abort.

        Raises ``UploadConflictError``, ``UploadTransientError``,
        ``UploadNameTooLongError``, ``UploadParentGoneError``, or
        ``UploadPermanentError``.
        """
        ...

    async def abort(self) -> None:
        """Discard the staged bytes; safe to call more than once."""
        ...


class UploadStaging(Protocol):
    """Starts resumable uploads for a backend."""

    async def begin(
        self,
        parent_id: NodeId,
        name: str,
        total_size: int,
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> UploadStage:
        """A new, empty stage for *name* under *parent_id*.

        Releases anything it allocated if it fails or is cancelled.
        """
        ...


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
    stage: UploadStage
    received: int = 0
    state: SessionState = SessionState.IDLE
    # Set when the session is removed while writing; the writer then aborts.
    cancelled: bool = False
    last_activity: float = 0.0


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
        staging: UploadStaging,
    ) -> None:
        self._store = store
        self._uploader = uploader
        self._staging = staging

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
        stage = await self._staging.begin(
            parent_id, name, total_size, mime_type=mime_type, media_info=media_info
        )
        # No await between begin and registration, so the stage cannot leak.
        session = UploadSession(
            session_id=secrets.token_urlsafe(32),
            parent_id=parent_id,
            name=name,
            total_size=total_size,
            mime_type=mime_type,
            media_info=media_info,
            stage=stage,
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
        """Abort every session; run before the backends close."""
        for session in self._store.pop_all():
            if session.state is SessionState.IDLE:
                await self._abort(session)
            else:
                session.cancelled = True

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
        session.last_activity = time.monotonic()
        try:
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
            if session.cancelled:
                await self._abort(session)

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
        await session.stage.write_at(session.received, data)
        session.received += len(data)
        session.last_activity = time.monotonic()

    async def _finalize_session(self, session: UploadSession) -> UploadCreatedNode:
        _L.debug(
            "begin finalising upload session session_id=%r name=%r parent_id=%r",
            session.session_id,
            session.name,
            session.parent_id,
        )
        try:
            record = await session.stage.commit()
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
            await session.stage.abort()
        except Exception:
            _L.warning(
                "failed to discard upload session session_id=%r name=%r",
                session.session_id,
                session.name,
                exc_info=True,
            )

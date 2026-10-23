"""Upload workflow service plus in-memory resumable session store."""

import asyncio
import os
import secrets
import tempfile
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Generator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, Protocol

from wcpan.drive.protocol.types import MediaInfo, MirrorStableId, NodeRecord

from ..lib.names import normalize_name


_L = getLogger(__name__)

FILE_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB for both reads and writes to temp file
UPLOAD_SESSION_TTL = 60 * 60
UPLOAD_CLEANUP_INTERVAL = 5 * 60


@dataclass
class UploadSession:
    session_id: str
    parent_id: MirrorStableId
    name: str
    total_size: int
    mime_type: str | None
    media_info: MediaInfo | None
    temp_path: Path
    received: int = 0
    uploading: bool = False
    last_activity: float = 0.0


class UploadSessionStore:
    def __init__(self, *, tmp_dir: Path) -> None:
        self._sessions: dict[str, UploadSession] = {}
        self._tmp_dir = tmp_dir

    def create(
        self,
        parent_id: MirrorStableId,
        name: str,
        total_size: int,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> UploadSession:
        session_id = secrets.token_urlsafe(32)
        fd, tmp_str = tempfile.mkstemp(
            prefix="wcpan_upload_",
            suffix=".tmp",
            dir=self._tmp_dir,
        )
        os.close(fd)
        session = UploadSession(
            session_id=session_id,
            parent_id=parent_id,
            name=name,
            total_size=total_size,
            mime_type=mime_type,
            media_info=media_info,
            temp_path=Path(tmp_str),
            last_activity=time.monotonic(),
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> UploadSession | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> UploadSession | None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.temp_path.unlink(missing_ok=True)
        return session

    def remove_expired(self, now: float, ttl: float) -> int:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if not session.uploading and now - session.last_activity >= ttl
        ]
        for session_id in expired:
            self.remove(session_id)
        return len(expired)

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.remove(session_id)


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


class UploadSessionNotFoundError(Exception):
    pass


class UploadInvalidChunkError(Exception):
    pass


class UploadTransientError(Exception):
    pass


class UploadPermanentError(Exception):
    pass


class UploadNameTooLongError(UploadPermanentError):
    def __init__(self, name: str) -> None:
        super().__init__(f"File name is too long for the backend: {name!r}")
        self.name = name


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

    async def find_conflict(
        self, parent_id: MirrorStableId, name: str
    ) -> NodeRecord | None:
        """The existing node named *name* under *parent_id*, if any."""
        ...

    async def upload(
        self,
        parent_id: MirrorStableId,
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


class UploadService:
    def __init__(
        self,
        *,
        store: UploadSessionStore,
        uploader: Uploader,
        executor: Executor | None = None,
    ) -> None:
        self._store = store
        self._uploader = uploader
        self._executor = executor

    async def _off_main[**A, R](
        self, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
    ) -> R:
        if self._executor is None:
            future = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        else:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, partial(fn, *args, **kwargs)
            )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running executor call cannot be cancelled. Keep the file and
            # its buffer alive until it finishes, then propagate cancellation.
            await future
            raise

    async def create_session(
        self,
        parent_id: MirrorStableId,
        name: str,
        total_size: int,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> UploadCreated:
        name = normalize_name(name)
        conflict = await self._uploader.find_conflict(parent_id, name)
        if conflict is not None:
            raise UploadConflictError(conflict)
        session = self._store.create(parent_id, name, total_size, mime_type, media_info)
        return UploadCreated(session_id=session.session_id, total_size=total_size)

    def get_session_status(self, session_id: str) -> SessionStatus | None:
        session = self._store.get(session_id)
        if session is None:
            return None
        session.last_activity = time.monotonic()
        return SessionStatus(received=session.received, total_size=session.total_size)

    def delete_session(self, session_id: str) -> bool:
        if self._store.remove(session_id) is None:
            return False
        return True

    async def cleanup_expired_sessions(self) -> None:
        while True:
            await asyncio.sleep(UPLOAD_CLEANUP_INTERVAL)
            removed = self._store.remove_expired(time.monotonic(), UPLOAD_SESSION_TTL)
            if removed:
                _L.info("removed %d expired upload session(s)", removed)

    async def upload_direct(
        self,
        parent_id: MirrorStableId,
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

        if session.uploading or start != session.received:
            raise UploadOffsetMismatchError(session.received)

        session.uploading = True
        session.last_activity = time.monotonic()
        try:
            with session.temp_path.open("r+b") as f:
                f.seek(session.received)
                buffer = bytearray(FILE_CHUNK_SIZE)
                buffered = 0
                async for chunk in content:
                    if session.received + buffered + len(chunk) > session.total_size:
                        raise UploadInvalidChunkError(
                            "Upload-Offset exceeds Upload-Length"
                        )
                    position = 0
                    while position < len(chunk):
                        copied = min(FILE_CHUNK_SIZE - buffered, len(chunk) - position)
                        buffer[buffered : buffered + copied] = chunk[
                            position : position + copied
                        ]
                        buffered += copied
                        position += copied
                        if buffered == FILE_CHUNK_SIZE:
                            await self._write_batch(f, buffer, buffered)
                            session.received += buffered
                            session.last_activity = time.monotonic()
                            buffered = 0
                if buffered:
                    await self._write_batch(f, buffer, buffered)
                    session.received += buffered
                    session.last_activity = time.monotonic()
            if session.received != session.total_size:
                return UploadAccepted(offset=session.received)
            return await self._finalize_session(session_id)
        except OSError as e:
            raise UploadPermanentError(f"Temporary upload storage failed: {e}") from e
        finally:
            session.uploading = False

    async def _write_batch(self, f: BinaryIO, buffer: bytearray, size: int) -> None:
        data = memoryview(buffer)[:size]
        written = await self._off_main(f.write, data)
        if written != size:
            raise OSError(f"Partial write: expected {size} bytes, wrote {written}")

    async def _finalize_session(
        self,
        session_id: str,
    ) -> UploadCreatedNode:
        session = self._store.get(session_id)
        if session is None:
            raise UploadSessionNotFoundError()

        _L.debug(
            "begin finalising upload session session_id=%r name=%r parent_id=%r",
            session_id,
            session.name,
            session.parent_id,
        )

        current_size = session.temp_path.stat().st_size
        if current_size != session.received or current_size != session.total_size:
            _L.warning(
                "upload session size mismatch before finalisation session_id=%r name=%r parent_id=%r received=%d file_size=%d total_size=%d",
                session_id,
                session.name,
                session.parent_id,
                session.received,
                current_size,
                session.total_size,
            )
            raise UploadPermanentError("Upload session size mismatch")

        try:
            with session.temp_path.open("rb") as f:
                record = await self._uploader.upload(
                    session.parent_id,
                    session.name,
                    self._iter_file(f),
                    mime_type=session.mime_type,
                    media_info=session.media_info,
                )
        except UploadConflictError:
            _L.debug(
                "upload session conflicted during finalisation session_id=%r name=%r parent_id=%r",
                session_id,
                session.name,
                session.parent_id,
            )
            self._cleanup_session(session_id)
            raise
        except UploadTransientError as e:
            _L.warning(
                "upload session failed during finalisation session_id=%r name=%r parent_id=%r error=%s",
                session_id,
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
                session_id,
                session.name,
                session.parent_id,
                e,
            )
            raise
        self._cleanup_session(session_id)

        _L.debug(
            "finalised upload session session_id=%r name=%r parent_id=%r id=%r",
            session_id,
            session.name,
            session.parent_id,
            record.id,
        )
        return UploadCreatedNode(record=record)

    async def _iter_file(self, f: BinaryIO) -> AsyncIterator[bytes]:
        while chunk := await self._off_main(f.read, FILE_CHUNK_SIZE):
            yield chunk

    def _cleanup_session(self, session_id: str) -> None:
        self._store.remove(session_id)


@contextmanager
def create_upload_service(
    *,
    tmp_dir: Path | None,
    uploader: Uploader,
) -> Generator[UploadService, None, None]:
    with tempfile.TemporaryDirectory(
        prefix="wcpan_upload_",
        dir=tmp_dir,
    ) as td:
        store = UploadSessionStore(tmp_dir=Path(td))
        with ThreadPoolExecutor(thread_name_prefix="wcpan-upload") as executor:
            try:
                yield UploadService(
                    store=store,
                    uploader=uploader,
                    executor=executor,
                )
            finally:
                store.close_all()

"""Resumable uploads for backends that only accept a whole file at once.

A backend that cannot write in place opts into ``SpooledFiles``: each file is a
local temporary file, and ``close`` streams it through the backend's one-shot
``Uploader``.
"""

import os
import tempfile
from collections.abc import AsyncIterator, Callable, Generator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from pathlib import Path
from typing import BinaryIO

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord

from ..lib.executor import allocate, run_to_completion
from .upload import FILE_CHUNK_SIZE, Uploader, UploadPermanentError


_L = getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SpoolArea:
    """Where spool files live, and the thread pool that writes them."""

    directory: Path
    executor: Executor

    async def run[**A, R](
        self, fn: Callable[A, R], /, *args: A.args, **kwargs: A.kwargs
    ) -> R:
        """Run *fn* on the pool; a cancelled caller still waits for it to finish.

        A running executor call cannot be cancelled, so this keeps the file it
        uses open until it is done.
        """
        return await run_to_completion(self.executor, fn, *args, **kwargs)

    async def allocate[R](
        self, fn: Callable[[], R], /, *, release: Callable[[R], None]
    ) -> R:
        """Run *fn* on the pool, releasing its result if the caller is cancelled."""
        return await allocate(self.executor, fn, release=release)


@contextmanager
def create_spool_area(*, tmp_dir: Path | None) -> Generator[SpoolArea, None, None]:
    """A private spool directory under *tmp_dir*, removed with its files on exit."""
    with tempfile.TemporaryDirectory(prefix="wcpan_upload_", dir=tmp_dir) as td:
        with ThreadPoolExecutor(thread_name_prefix="wcpan-upload") as executor:
            yield SpoolArea(directory=Path(td), executor=executor)


def _storage_error(e: OSError) -> UploadPermanentError:
    return UploadPermanentError(f"Temporary upload storage failed: {e}")


class SpooledFiles:
    """``FileOpener`` that spools each file locally, then uploads it whole."""

    def __init__(self, *, uploader: Uploader, area: SpoolArea) -> None:
        self._uploader = uploader
        self._area = area

    async def open(
        self,
        parent_id: NodeId,
        name: str,
        size: int,
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> "SpooledFile":
        try:
            path, f = await self._area.allocate(
                partial(_create_spool_file, self._area.directory),
                release=_discard_spool_file,
            )
        except OSError as e:
            raise _storage_error(e) from e
        return SpooledFile(
            uploader=self._uploader,
            area=self._area,
            path=path,
            file=f,
            parent_id=parent_id,
            name=name,
            size=size,
            mime_type=mime_type,
            media_info=media_info,
        )


class SpooledFile:
    """``WritableFile`` over a local spool file; ``close`` uploads it."""

    def __init__(
        self,
        *,
        uploader: Uploader,
        area: SpoolArea,
        path: Path,
        file: BinaryIO,
        parent_id: NodeId,
        name: str,
        size: int,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> None:
        self._uploader = uploader
        self._area = area
        self.path = path
        self._file: BinaryIO | None = file
        self._position = 0
        self._parent_id = parent_id
        self._name = name
        self._size = size
        self._mime_type = mime_type
        self._media_info = media_info

    async def seek(self, offset: int) -> None:
        self._require_file()
        if offset < 0:
            raise ValueError(f"negative offset {offset}")
        self._position = offset

    async def write(self, data: bytes) -> int:
        f = self._require_file()
        try:
            kept = await self._area.run(_write_at, f, self._position, data)
        except OSError as e:
            raise _storage_error(e) from e
        self._position += kept
        return kept

    async def flush(self) -> None:
        f = self._require_file()
        try:
            await self._area.run(f.flush)
        except OSError as e:
            raise _storage_error(e) from e

    async def close(self) -> NodeRecord:
        f = self._require_file()
        try:
            size = await self._area.run(_flushed_size, f)
        except OSError as e:
            raise _storage_error(e) from e
        if size != self._size:
            _L.warning(
                "upload spool size mismatch before finalisation name=%r parent_id=%r file_size=%d total_size=%d",
                self._name,
                self._parent_id,
                size,
                self._size,
            )
            raise UploadPermanentError("Upload session size mismatch")

        try:
            reader = await self._area.allocate(
                partial(open, self.path, "rb"), release=_close
            )
        except OSError as e:
            raise _storage_error(e) from e
        try:
            record = await self._uploader.upload(
                self._parent_id,
                self._name,
                self._iter_file(reader),
                mime_type=self._mime_type,
                media_info=self._media_info,
            )
        finally:
            await self._area.run(reader.close)
        await self.abort()
        return record

    async def abort(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            await self._area.run(_discard_spool_file, (self.path, f))

    def _require_file(self) -> BinaryIO:
        if self._file is None:
            raise UploadPermanentError("Upload session was already closed")
        return self._file

    async def _iter_file(self, f: BinaryIO) -> AsyncIterator[bytes]:
        while chunk := await self._area.run(f.read, FILE_CHUNK_SIZE):
            yield chunk


def _create_spool_file(directory: Path) -> tuple[Path, BinaryIO]:
    fd, name = tempfile.mkstemp(prefix="wcpan_upload_", suffix=".tmp", dir=directory)
    return Path(name), os.fdopen(fd, "r+b")


def _write_at(f: BinaryIO, offset: int, data: bytes) -> int:
    f.seek(offset)
    return f.write(data)


def _flushed_size(f: BinaryIO) -> int:
    f.flush()
    return os.fstat(f.fileno()).st_size


def _close(f: BinaryIO) -> None:
    f.close()


def _discard_spool_file(spool: tuple[Path, BinaryIO]) -> None:
    path, f = spool
    f.close()
    path.unlink(missing_ok=True)

"""Resumable uploads for backends that only accept a whole file at once.

A backend that cannot write in place opts into ``SpooledFiles``: each file is a
local temporary file, and ``close`` streams it through the backend's one-shot
``Uploader``.

Each spool area is a private directory locked for as long as it is in use. A
new area first removes the areas under the same parent that no process holds,
which a crash left behind.
"""

import fcntl
import os
import shutil
import tempfile
import time
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

_AREA_PREFIX = "wcpan_upload_"
_LOCK_NAME = ".lock"
# An area without a lock file is still being created, or predates the lock;
# remove it only once it is this many seconds old.
_UNLOCKED_GRACE = 60 * 60


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
    """A private spool directory under *tmp_dir*, removed with its files on exit.

    Abandoned areas under *tmp_dir* are removed first.
    """
    sweep_spool_areas(tmp_dir or Path(tempfile.gettempdir()))
    directory = Path(tempfile.mkdtemp(prefix=_AREA_PREFIX, dir=tmp_dir))
    try:
        lock = os.open(
            directory / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    try:
        # Held until the directory is gone, so no sweep removes it while in use.
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(thread_name_prefix="wcpan-upload") as executor:
            yield SpoolArea(directory=directory, executor=executor)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        os.close(lock)


def sweep_spool_areas(parent: Path) -> None:
    """Remove the spool areas under *parent* that no process holds."""
    try:
        children = list(parent.iterdir())
    except OSError as e:
        _L.warning("cannot look for abandoned upload spools in %s: %s", parent, e)
        return
    for child in children:
        if child.name.startswith(_AREA_PREFIX) and _abandoned_area(child):
            shutil.rmtree(child, ignore_errors=True)
            _L.info("removed abandoned upload spool %s", child)


def _abandoned_area(path: Path) -> bool:
    try:
        if not path.is_dir(follow_symlinks=False):
            return False
        lock = os.open(path / _LOCK_NAME, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        try:
            return time.time() - path.lstat().st_mtime > _UNLOCKED_GRACE
        except OSError:
            return False
    except OSError:
        return False  # Another user's, or unreadable.
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # Held by a live process.
    finally:
        # Closing drops the lock; a live owner never unlocks before removing.
        os.close(lock)
    return True


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
        self._parent_id = parent_id
        self._name = name
        self._size = size
        self._mime_type = mime_type
        self._media_info = media_info

    async def write(self, offset: int, data: bytes) -> int:
        f = self._require_file()
        try:
            return await self._area.run(_write_at, f, offset, data)
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

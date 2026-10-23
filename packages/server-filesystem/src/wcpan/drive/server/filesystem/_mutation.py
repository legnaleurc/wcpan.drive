"""Blocking filesystem changes, run on the backend's own thread pool."""

import ctypes
import ctypes.util
import errno
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Sequence
from typing import Any, BinaryIO

from ._reading import STAGING_PREFIX, new_digest


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_CHUNK_SIZE = 1024 * 1024


def _load_renameat2() -> Callable[..., int] | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    fn: Any = getattr(ctypes.CDLL(name, use_errno=True), "renameat2", None)
    if fn is None:
        return None
    fn.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    fn.restype = ctypes.c_int
    return fn


_renameat2 = _load_renameat2()


def rename_no_replace(src: str, dst: str) -> None:
    """Rename *src* to *dst*, failing with ``FileExistsError`` if *dst* exists.

    Filesystems without ``RENAME_NOREPLACE`` fall back to a check, which can
    race with another writer.
    """
    if _renameat2 is not None:
        if not _renameat2(
            _AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE
        ):
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(code, os.strerror(code), src, None, dst)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst)


def make_directory(path: str) -> None:
    os.mkdir(path)


def remove(path: str, *, is_directory: bool) -> None:
    """Delete *path* permanently; a directory is removed with its contents."""
    if is_directory:
        # rmtree removes symlinks inside the tree without following them.
        shutil.rmtree(path)
    else:
        os.unlink(path)


def create_staging(directory: str) -> tuple[str, BinaryIO]:
    """A new, empty staging file in *directory*, open for reading and writing."""
    path = os.path.join(directory, f"{STAGING_PREFIX}{uuid.uuid4().hex}")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    return path, os.fdopen(os.open(path, flags, 0o666), "r+b")


def remove_stale_staging(paths: Sequence[str], *, before: float) -> list[str]:
    """Delete the regular files among *paths* last modified before *before*.

    Returns the paths removed; ones that changed or vanished are skipped.
    """
    removed: list[str] = []
    for path in paths:
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_mtime >= before:
                continue
            os.unlink(path)
        except FileNotFoundError:
            continue
        removed.append(path)
    return removed


def discard_new_staging(staging: tuple[str, BinaryIO]) -> None:
    """Close and delete a staging file from ``create_staging``."""
    path, f = staging
    f.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class StagedFile:
    """An open staging file, hashed as it is written in order.

    Every method blocks; call them on the backend's pool, one at a time.
    """

    def __init__(self, f: BinaryIO) -> None:
        self.file = f
        self._digest = new_digest()
        self._hashed = 0
        self._in_order = True

    def write_at(self, offset: int, data: bytes) -> int:
        """Write *data* at *offset*; return how many bytes were written."""
        self.file.seek(offset)
        written = self.file.write(data)
        # Updated in the same call as the write, so a caller cancelled while it
        # ran cannot leave the two apart.
        if self._in_order:
            if offset == self._hashed:
                self._digest.update(memoryview(data)[:written])
                self._hashed += written
            else:
                # A gap, or a range written again, perhaps with other bytes.
                self._in_order = False
        return written

    def finish(self) -> str:
        """Flush the content to disk and return its checksum."""
        self.file.flush()
        os.fsync(self.file.fileno())
        if self._in_order and self._hashed == os.fstat(self.file.fileno()).st_size:
            return self._digest.hexdigest()
        digest = new_digest()
        self.file.seek(0)
        while chunk := self.file.read(_CHUNK_SIZE):
            digest.update(chunk)
        return digest.hexdigest()

    def is_at(self, path: str) -> bool:
        """Whether *path* still names this file."""
        try:
            return os.path.samestat(os.lstat(path), os.fstat(self.file.fileno()))
        except FileNotFoundError:
            return False

    def discard(self, paths: Sequence[str]) -> None:
        """Close the file and delete it from the first of *paths* that names it."""
        st = os.fstat(self.file.fileno())
        self.file.close()
        for path in paths:
            try:
                if os.path.samestat(os.lstat(path), st):
                    os.unlink(path)
                    return
            except FileNotFoundError:
                continue

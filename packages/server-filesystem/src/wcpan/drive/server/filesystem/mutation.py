"""Blocking filesystem changes, run on the backend's own thread pool."""

import ctypes
import ctypes.util
import errno
import os
import shutil
import uuid
from collections.abc import Callable
from typing import Any, BinaryIO

from .reading import STAGING_PREFIX


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


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
    """A new, empty staging file in *directory*, open for writing."""
    path = os.path.join(directory, f"{STAGING_PREFIX}{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    return path, os.fdopen(os.open(path, flags, 0o666), "wb")


def finish_staging(f: BinaryIO) -> None:
    """Flush staged content to disk before it is renamed into place."""
    f.flush()
    os.fsync(f.fileno())
    f.close()


def discard_staging(path: str, f: BinaryIO) -> None:
    f.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

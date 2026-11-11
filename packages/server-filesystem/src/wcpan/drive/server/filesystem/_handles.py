"""Object identity from Linux file handles, falling back to inode numbers.

A handle from ``name_to_handle_at()`` names the object, not the path: it survives
renames and is shared by hard links. Handles are qualified by the filesystem ID
and stored without interpreting their bytes. Filesystems without handle support
fall back to the inode number, which can be reused after deletion.
"""

import ctypes
import ctypes.util
import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NewType


FileIdentity = NewType("FileIdentity", str)
"""``<fsid>:h<type>:<hex>`` for a handle, ``<fsid>:ino:<number>`` otherwise."""

_AT_FDCWD = -100
_MAX_HANDLE_SZ = 128
# Filesystems that cannot export handles; anything else is a real failure.
_UNSUPPORTED = {errno.EOPNOTSUPP, errno.ENOSYS, errno.EOVERFLOW}


class _FileHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint),
        ("handle_type", ctypes.c_int),
        ("f_handle", ctypes.c_ubyte * _MAX_HANDLE_SZ),
    ]


def _load_name_to_handle_at() -> Callable[..., int] | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    fn: Any = getattr(ctypes.CDLL(name, use_errno=True), "name_to_handle_at", None)
    if fn is None:
        return None
    fn.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_FileHandle),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    fn.restype = ctypes.c_int
    return fn


_name_to_handle_at = _load_name_to_handle_at()


@dataclass(frozen=True, slots=True)
class _Handle:
    handle_type: int
    data: bytes


def _handle(path: str) -> _Handle | None:
    """The file handle of *path* (not following symlinks), or ``None`` if unsupported."""
    if _name_to_handle_at is None:
        return None
    fh = _FileHandle()
    fh.handle_bytes = _MAX_HANDLE_SZ
    mount_id = ctypes.c_int()
    if _name_to_handle_at(
        _AT_FDCWD, os.fsencode(path), ctypes.byref(fh), ctypes.byref(mount_id), 0
    ):
        code = ctypes.get_errno()
        if code in _UNSUPPORTED:
            return None
        raise OSError(code, os.strerror(code), path)
    return _Handle(fh.handle_type, bytes(fh.f_handle[: fh.handle_bytes]))


def filesystem_id(path: str) -> str:
    """Persistent identity of the filesystem holding *path*.

    ``f_fsid`` is derived from the filesystem UUID on ext4, unlike the mount ID or
    device number, which can change across reboots.
    """
    return format(os.statvfs(path).f_fsid, "x")


def identify(path: str, st: os.stat_result, fsid: str) -> FileIdentity:
    """Identity of the object at *path*, which *st* describes."""
    handle = _handle(path)
    if handle is None:
        return FileIdentity(f"{fsid}:ino:{st.st_ino}")
    return FileIdentity(f"{fsid}:h{handle.handle_type}:{handle.data.hex()}")

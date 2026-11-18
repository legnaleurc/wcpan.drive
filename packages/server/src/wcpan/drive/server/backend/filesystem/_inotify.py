"""Minimal inotify binding through ctypes.

Events are hints that a directory needs reading again; they carry no metadata
the mirror relies on.
"""

import ctypes
import ctypes.util
import os
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any


IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000

_IN_CLOEXEC = os.O_CLOEXEC
_IN_NONBLOCK = os.O_NONBLOCK

# Changes to a directory's entries or to their metadata. Content written without
# closing (for example through mmap) is only picked up by a rescan.
DIRECTORY_MASK = (
    IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)

_EVENT = struct.Struct("iIII")


@dataclass(frozen=True, slots=True)
class Event:
    wd: int
    mask: int
    name: str


def _load() -> tuple[Callable[..., int], Callable[..., int], Callable[..., int]] | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    libc = ctypes.CDLL(name, use_errno=True)
    init: Any = getattr(libc, "inotify_init1", None)
    add: Any = getattr(libc, "inotify_add_watch", None)
    rm: Any = getattr(libc, "inotify_rm_watch", None)
    if init is None or add is None or rm is None:
        return None
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    rm.argtypes = [ctypes.c_int, ctypes.c_int]
    rm.restype = ctypes.c_int
    return init, add, rm


_libc = _load()


def _check(result: int, path: str | None = None) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return result


class Inotify:
    """A non-blocking inotify instance; read events when its fd is readable."""

    def __init__(self) -> None:
        if _libc is None:
            raise OSError("inotify is not available")
        self._init, self._add, self._rm = _libc
        self._fd = _check(self._init(_IN_CLOEXEC | _IN_NONBLOCK))

    @property
    def fd(self) -> int:
        return self._fd

    def add_watch(self, path: str, mask: int = DIRECTORY_MASK) -> int:
        """Watch *path*; the same directory always returns the same descriptor."""
        return _check(self._add(self._fd, os.fsencode(path), mask), path)

    def rm_watch(self, wd: int) -> None:
        self._rm(self._fd, wd)

    def read_events(self) -> Iterator[Event]:
        """All events queued so far; empty when none are pending."""
        while True:
            try:
                data = os.read(self._fd, 64 * 1024)
            except BlockingIOError:
                return
            offset = 0
            while offset + _EVENT.size <= len(data):
                wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                offset += _EVENT.size
                raw = data[offset : offset + length]
                offset += length
                yield Event(wd, mask, os.fsdecode(raw.rstrip(b"\0")))

    def close(self) -> None:
        os.close(self._fd)

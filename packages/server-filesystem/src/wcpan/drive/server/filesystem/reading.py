"""Blocking filesystem reads, run on the backend's own thread pool."""

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import BinaryIO

from .handles import FileIdentity, filesystem_id, identify
from .store import EntryState


HASH_ALGORITHM = "md5"
"""Server-computed checksum for files; the filesystem provides none."""

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Entry:
    """One directory entry: a regular file or a directory."""

    name: str
    path: str
    is_directory: bool
    stat: os.stat_result
    identity: FileIdentity

    @property
    def state(self) -> EntryState:
        return entry_state(self.identity, self.stat)


def entry_state(identity: FileIdentity, st: os.stat_result) -> EntryState:
    return EntryState(identity, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class _FsidCache:
    """Filesystem IDs by device; a directory tree can contain other mounts."""

    def __init__(self) -> None:
        self._by_dev: dict[int, str] = {}

    def get(self, path: str, st: os.stat_result) -> str:
        fsid = self._by_dev.get(st.st_dev)
        if fsid is None:
            fsid = filesystem_id(path)
            self._by_dev[st.st_dev] = fsid
        return fsid


_fsids = _FsidCache()


def stat_entry(path: str) -> Entry | None:
    """Describe *path* without following symlinks; ``None`` for other file types.

    Raises ``OSError`` when the path cannot be read.
    """
    st = os.lstat(path)
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        return None
    return Entry(
        name=os.path.basename(path),
        path=path,
        is_directory=stat.S_ISDIR(st.st_mode),
        stat=st,
        identity=identify(path, st, _fsids.get(path, st)),
    )


def list_directory(path: str) -> list[Entry]:
    """Regular files and directories directly under *path*.

    Entries that vanish while listing are skipped; failing to list *path* itself
    raises ``OSError``.
    """
    entries: list[Entry] = []
    with os.scandir(path) as it:
        for dirent in it:
            try:
                entry = stat_entry(dirent.path)
            except FileNotFoundError:
                continue
            if entry is not None:
                entries.append(entry)
    return entries


def hash_file(path: str, expected: EntryState) -> str | None:
    """Checksum of *path* if it still matches *expected* before and after reading.

    Returns ``None`` when the file changed or was replaced, so the caller can
    withhold the observation and retry later.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as f:
        if not _unchanged(os.fstat(f.fileno()), expected):
            return None
        digest = hashlib.md5(usedforsecurity=False)
        while chunk := f.read(_CHUNK_SIZE):
            digest.update(chunk)
        if not _unchanged(os.fstat(f.fileno()), expected):
            return None
    return digest.hexdigest()


def _unchanged(st: os.stat_result, expected: EntryState) -> bool:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns) == (
        expected.size,
        expected.mtime_ns,
        expected.ctime_ns,
    )


def open_verified(path: str, identity: FileIdentity) -> BinaryIO:
    """Open *path* for reading if it still names *identity*.

    Raises ``FileNotFoundError`` when the path now names another object.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    f = os.fdopen(os.open(path, flags), "rb")
    try:
        st = os.fstat(f.fileno())
        current = os.lstat(path)
        same_object = (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino)
        if not same_object or identify(path, st, _fsids.get(path, st)) != identity:
            raise FileNotFoundError(path)
    except BaseException:
        f.close()
        raise
    return f

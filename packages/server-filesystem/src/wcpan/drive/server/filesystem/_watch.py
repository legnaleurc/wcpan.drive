"""Collect directories whose entries changed, from inotify hints.

Watches map to directory node IDs rather than paths: a watch follows its
directory through renames, and the path is rebuilt from the mirror when the
directory is read again.
"""

import asyncio
import errno
from logging import getLogger

from wcpan.drive.protocol.types import NodeId

from ._inotify import IN_IGNORED, IN_Q_OVERFLOW, Inotify


_L = getLogger(__name__)


class DirectoryWatcher:
    def __init__(self, inotify: Inotify) -> None:
        self._inotify = inotify
        self._nodes: dict[int, NodeId] = {}
        self._dirty = set[NodeId]()
        self._overflow = False
        self._changed = asyncio.Event()
        self._limit_reported = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def create(cls) -> "DirectoryWatcher | None":
        """A watcher, or ``None`` when inotify is unavailable."""
        try:
            return cls(Inotify())
        except OSError as e:
            _L.warning("Cannot watch for changes; relying on rescans: %s", e)
            return None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._inotify.fd, self._on_readable)

    def close(self) -> None:
        if self._loop is not None:
            self._loop.remove_reader(self._inotify.fd)
        self._inotify.close()

    def watch(self, node_id: NodeId, path: str) -> None:
        """Watch the directory at *path*; call before listing it."""
        try:
            wd = self._inotify.add_watch(path)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return
            if not self._limit_reported:
                self._limit_reported = True
                _L.warning(
                    "Cannot watch %s; changes there wait for the next rescan"
                    " (raise fs.inotify.max_user_watches if the limit is reached): %s",
                    path,
                    e,
                )
            return
        self._nodes[wd] = node_id

    async def wait(self) -> None:
        """Return once some directory changed or events were lost."""
        await self._changed.wait()

    def take(self) -> tuple[set[NodeId], bool]:
        """Changed directories, and whether events were lost since the last take."""
        dirty, overflow = self._dirty, self._overflow
        self._dirty = set[NodeId]()
        self._overflow = False
        self._changed.clear()
        return dirty, overflow

    def _on_readable(self) -> None:
        for event in self._inotify.read_events():
            if event.mask & IN_Q_OVERFLOW:
                self._overflow = True
                continue
            if event.mask & IN_IGNORED:
                self._nodes.pop(event.wd, None)
                continue
            node_id = self._nodes.get(event.wd)
            if node_id is not None:
                self._dirty.add(node_id)
        if self._dirty or self._overflow:
            self._changed.set()

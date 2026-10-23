"""Media probing reads files where they are, bound to the observed state."""

import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial
from typing import BinaryIO

from wcpan.drive.server.exceptions import MediaSourceError
from wcpan.drive.server.lib import pread_stream
from wcpan.drive.server.types import (
    BackendLocator,
    MediaSource,
    NodeObservation,
    OpenMediaSource,
)

from ._blocking import Blocking
from ._reading import open_verified
from ._store import EntryState


class FileMediaSource:
    """Ranged reads of an open file that still matches its observed state."""

    def __init__(
        self, *, file: BinaryIO, state: EntryState, blocking: Blocking
    ) -> None:
        self._file = file
        self._state = state
        self._blocking = blocking

    @property
    def size(self) -> int:
        return self._state.size

    def local_fd(self) -> int | None:
        # On Linux, opening /dev/fd/N opens the same inode, so MediaInfo reads
        # the verified file whatever happened to its path. Elsewhere it may
        # duplicate the descriptor or not exist; probe over HTTP there.
        return self._file.fileno() if sys.platform == "linux" else None

    def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        return pread_stream(self._pread, offset, length)

    async def _pread(self, size: int, position: int) -> bytes:
        # Finished even when cancelled, as the file closes afterwards.
        return await self._blocking.to_completion(
            os.pread, self._file.fileno(), size, position
        )

    async def unchanged(self) -> bool:
        st = await self._blocking.to_completion(os.fstat, self._file.fileno())
        return _matches(st, self._state)


def media_sources(blocking: Blocking) -> OpenMediaSource:
    """Opens the file an observation names, if it still matches what was observed.

    The size, modification time, and change time are checked when it opens and
    again after the probe's last read.
    """

    @asynccontextmanager
    async def open_source(
        observation: NodeObservation,
    ) -> AsyncGenerator[MediaSource, None]:
        path = observation.source_key
        locator = observation.locator
        if path is None or locator is None:
            raise MediaSourceError(f"{observation.record.id} has no path or state")
        state = EntryState.from_locator(BackendLocator(locator))
        file = await blocking.allocate(
            partial(open_verified, path, state.identity), release=_close
        )
        try:
            st = await blocking.to_completion(os.fstat, file.fileno())
            if not _matches(st, state):
                raise MediaSourceError(f"{path} changed since it was observed")
            yield FileMediaSource(file=file, state=state, blocking=blocking)
        finally:
            await blocking.to_completion(file.close)

    return open_source


def _close(file: BinaryIO) -> None:
    file.close()


def _matches(st: os.stat_result, state: EntryState) -> bool:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns) == (
        state.size,
        state.mtime_ns,
        state.ctime_ns,
    )

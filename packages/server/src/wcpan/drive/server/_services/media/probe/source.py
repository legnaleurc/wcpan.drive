"""What a probe reads from, and how a probe can fail."""

import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Executor
from contextlib import aclosing
from typing import BinaryIO, Protocol

from ...._lib.executor import run_to_completion
from ...._types import ProbeFailure


# Bytes of a positional read at a time.
CHUNK_SIZE = 256 * 1024


class MediaSource(Protocol):
    """Exact ranged reads of one file of a known size.

    Opened by a backend for one observation, and closed when the probe ends.

    A source may also offer ``local_fd() -> int | None``: a descriptor of the
    regular file it opened and verified, which MediaInfo then reads directly as
    ``/dev/fd/N`` instead of over the source service. Only for sources whose
    descriptor opens the same file on this platform.
    """

    @property
    def size(self) -> int: ...

    def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        """Exactly *length* bytes at *offset*, which lie within the file.

        Positional: streams are independent and may overlap. A remote source
        makes one upstream request per stream, read in bounded chunks; closing
        the stream early (``aclose``) stops that request. Raises
        ``MediaSourceError`` (or any other exception) when it cannot finish;
        never ends with fewer bytes.
        """
        ...

    async def unchanged(self) -> bool:
        """Whether the file still matches the observation it was opened for.

        Called after the last read; dimensions read from a changed file are
        discarded.
        """
        ...


class MediaSourceError(Exception):
    """A source could not be opened or read."""


class MediaProbeError(Exception):
    def __init__(self, kind: ProbeFailure, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


class ProbeUnavailableError(RuntimeError):
    """MediaInfo cannot be run; a service error, not a per-file failure."""


async def read_exactly(source: MediaSource, offset: int, length: int) -> bytes:
    """All of ``source.stream(offset, length)``, checked for its length."""
    async with aclosing(source.stream(offset, length)) as chunks:
        data = b"".join([chunk async for chunk in chunks])
    if len(data) != length:
        raise MediaSourceError(f"read {len(data)} of {length} bytes at {offset}")
    return data


async def pread_stream(
    pread: Callable[[int, int], Awaitable[bytes]], offset: int, length: int
) -> AsyncGenerator[bytes, None]:
    """Stream *length* bytes at *offset* from ``pread(size, position)``.

    For local files: a regular file reads short only at its end, which within
    the observed size means the file shrank.
    """
    position = offset
    end = offset + length
    while position < end:
        data = await pread(min(CHUNK_SIZE, end - position), position)
        if not data:
            raise MediaSourceError(
                f"read {position - offset} of {length} bytes at {offset}"
            )
        position += len(data)
        yield data


class LocalFileSource:
    """A ``MediaSource`` over an open local file, read on *executor*.

    For files the server holds itself, such as upload spools and staging files,
    which nothing else changes meanwhile. Reads run to completion even when the
    probe is cancelled, so the caller may close the file afterwards.
    """

    def __init__(self, file: BinaryIO, size: int, *, executor: Executor) -> None:
        self._fd = file.fileno()
        self._size = size
        self._executor = executor

    @property
    def size(self) -> int:
        return self._size

    def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        return pread_stream(self._pread, offset, length)

    async def _pread(self, size: int, position: int) -> bytes:
        return await run_to_completion(
            self._executor, os.pread, self._fd, size, position
        )

    async def unchanged(self) -> bool:
        st = await run_to_completion(self._executor, os.fstat, self._fd)
        return st.st_size == self._size

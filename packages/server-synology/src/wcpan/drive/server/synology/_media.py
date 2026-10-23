"""Media probing reads files over the Drive API with ranged downloads.

A probe is bound to the size, modification time, and content hash the
observation saw, and the file is looked up again after the last read. A change
the API does not report yet goes unnoticed.
"""

from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager

from wcpan.drive.protocol.lib import parse_content_range
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.exceptions import MediaSourceError
from wcpan.drive.server.lib import CHUNK_SIZE
from wcpan.drive.server.types import MediaSource, NodeObservation, OpenMediaSource
from wcpan.synology.exceptions import SynologyNetworkError
from wcpan.synology.types import (
    SynologyClient,
    SynologyPermanentLink,
)

from ._lookup import find_file


class DriveMediaSource:
    """Exact ranged reads of one file, checked against what was observed."""

    def __init__(
        self, *, drive_api: SynologyClient, link: str, record: NodeRecord
    ) -> None:
        self._drive_api = drive_api
        self._ref = SynologyPermanentLink(permanent_link=link)
        self._record = record

    @property
    def size(self) -> int:
        return self._record.size

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        # One ranged download per stream, read in bounded chunks; closing the
        # stream early releases the response.
        try:
            async with aclosing(self._stream(offset, length)) as chunks:
                async for chunk in chunks:
                    yield chunk
        except SynologyNetworkError as e:
            raise MediaSourceError(str(e)) from e

    async def _stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        end = offset + length
        async with self._drive_api.download(
            self._ref, range_=slice(offset, end)
        ) as response:
            # A full response or a JSON error (an expired session) is not the range.
            if response.status != 206:
                raise MediaSourceError(
                    f"expected 206 for a range, got {response.status}"
                )
            content_range = response.headers.get("Content-Range", "")
            if parse_content_range(content_range) != (offset, end - 1, self.size):
                raise MediaSourceError(
                    f"expected Content-Range 'bytes {offset}-{end - 1}/{self.size}', "
                    f"got {content_range!r}"
                )
            received = 0
            chunks = response.content.iter_chunked(CHUNK_SIZE)
            while True:
                try:
                    chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                except Exception as e:
                    raise MediaSourceError(
                        f"range {offset}-{end - 1} failed after {received} bytes"
                        f" ({type(e).__name__})"
                    ) from None
                received += len(chunk)
                if received > length:
                    raise MediaSourceError(f"range {offset}-{end - 1} is too long")
                yield chunk
            if received < length:
                raise MediaSourceError(
                    f"range {offset}-{end - 1} ended after {received} bytes"
                )

    async def unchanged(self) -> bool:
        try:
            info = await find_file(self._drive_api, self._ref)
        except SynologyNetworkError as e:
            raise MediaSourceError(str(e)) from e
        if info is None:
            return False
        record = self._record
        return (
            info.get("size", 0) == record.size
            and info.get("modified_time", 0) == record.modified_time
            and info.get("hash", "") == record.hash
        )


def media_sources(drive_api: SynologyClient) -> OpenMediaSource:
    """Opens files by their permanent links, the observations' source keys."""

    @asynccontextmanager
    async def open_source(
        observation: NodeObservation,
    ) -> AsyncGenerator[MediaSource, None]:
        link = observation.source_key
        if link is None:
            raise MediaSourceError(f"{observation.record.id} has no permanent link")
        yield DriveMediaSource(
            drive_api=drive_api, link=link, record=observation.record
        )

    return open_source

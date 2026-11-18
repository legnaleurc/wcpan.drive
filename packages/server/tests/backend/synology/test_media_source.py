"""Media probes read Synology files with ranged downloads."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase

from aiohttp import StreamReader
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._services.media.probe.source import (
    MediaSourceError,
    read_exactly,
)
from wcpan.drive.server._types import NodeObservation
from wcpan.drive.server.backend.synology._media import media_sources
from wcpan.synology.exceptions import (
    SynologyNetworkError,
    SynologyNotFoundError,
)
from wcpan.synology.types import SynologyClient

from ._lib import make_stream


_DATA = b"0123456789"


@dataclass
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes
    content: StreamReader = field(init=False)

    def __post_init__(self) -> None:
        self.content = make_stream(self.body)


class _Drive:
    """Answers ranged downloads as Synology does, or as configured."""

    def __init__(self) -> None:
        self.response: _Response | None = None
        self.open_downloads = 0
        self.info: dict[str, Any] | None = {
            "size": len(_DATA),
            "modified_time": 100,
            "hash": "h1",
        }

    @asynccontextmanager
    async def download(
        self, ref: object, *, range_: slice
    ) -> AsyncGenerator[_Response, None]:
        self.open_downloads += 1
        try:
            if self.response is not None:
                yield self.response
                return
            start, stop = range_.start, range_.stop
            yield _Response(
                206,
                {"Content-Range": f"bytes {start}-{stop - 1}/{len(_DATA)}"},
                _DATA[start:stop],
            )
        finally:
            self.open_downloads -= 1

    async def get_file(self, ref: object) -> dict[str, Any]:
        if self.info is None:
            raise SynologyNotFoundError("missing", error_code=1003)
        return self.info


def _observation() -> NodeObservation:
    record = NodeRecord(
        id=NodeId("link"),
        parent_id=NodeId("p"),
        name="a.jpg",
        is_directory=False,
        created_time=100,
        modified_time=100,
        changed_time=100,
        mime_type="image/jpeg",
        hash="h1",
        hash_algorithm="md4",
        size=len(_DATA),
        is_image=True,
        is_video=False,
        width=0,
        height=0,
    )
    return NodeObservation(record=record, locator=None, source_key="link")


class TestDriveMediaSource(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.drive = _Drive()
        self.open_source = media_sources(cast(SynologyClient, self.drive))

    async def read(self, offset: int, length: int) -> bytes:
        async with self.open_source(_observation()) as source:
            return await read_exactly(source, offset, length)

    async def test_reads_exact_ranges(self) -> None:
        # when / then
        self.assertEqual(await self.read(3, 4), b"3456")
        self.assertEqual(self.drive.open_downloads, 0)

    async def test_closing_a_stream_early_releases_the_download(self) -> None:
        async with self.open_source(_observation()) as source:
            # given
            stream = source.stream(0, len(_DATA))
            await anext(stream)
            self.assertEqual(self.drive.open_downloads, 1)

            # when
            await stream.aclose()

            # then
            self.assertEqual(self.drive.open_downloads, 0)

    async def test_full_response_is_not_a_range(self) -> None:
        # given
        self.drive.response = _Response(200, {}, _DATA)
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_json_error_is_not_a_range(self) -> None:
        # given: an expired session answers 200 with a JSON error
        self.drive.response = _Response(
            200,
            {"Content-Type": "application/json"},
            b'{"success":false,"error":{"code":119}}',
        )
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_other_range_is_rejected(self) -> None:
        # given
        self.drive.response = _Response(
            206, {"Content-Range": f"bytes 0-3/{len(_DATA)}"}, _DATA[:4]
        )
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_other_total_size_is_rejected(self) -> None:
        # given: the file grew since it was observed
        self.drive.response = _Response(
            206, {"Content-Range": "bytes 3-6/20"}, _DATA[3:7]
        )
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_short_body_is_rejected(self) -> None:
        # given
        self.drive.response = _Response(
            206, {"Content-Range": f"bytes 3-6/{len(_DATA)}"}, _DATA[3:6]
        )
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_long_body_is_rejected(self) -> None:
        # given
        self.drive.response = _Response(
            206, {"Content-Range": f"bytes 3-6/{len(_DATA)}"}, _DATA[3:8]
        )
        # when / then
        with self.assertRaises(MediaSourceError):
            await self.read(3, 4)

    async def test_recheck_network_error_is_unavailable(self) -> None:
        # given
        error = SynologyNetworkError("SYNO.SynologyDrive.Files.get failed: HTTP 502")

        async def failing(ref: object) -> None:
            raise error

        self.drive.get_file = failing  # type: ignore[method-assign]
        async with self.open_source(_observation()) as source:
            # when
            with self.assertRaises(MediaSourceError) as cm:
                await source.unchanged()
        # then
        self.assertIs(cm.exception.__cause__, error)

    async def test_unchanged_compares_size_time_and_hash(self) -> None:
        for change, expected in [
            ({}, True),
            ({"size": 11}, False),
            ({"modified_time": 101}, False),
            ({"hash": "h2"}, False),
        ]:
            with self.subTest(change=change):
                # given
                assert self.drive.info is not None
                self.drive.info = {**self.drive.info, **change}
                # when
                async with self.open_source(_observation()) as source:
                    result = await source.unchanged()
                # then
                self.assertEqual(result, expected)
                self.drive.info = {
                    "size": len(_DATA),
                    "modified_time": 100,
                    "hash": "h1",
                }

    async def test_removed_file_is_changed(self) -> None:
        # given
        self.drive.info = None
        # when
        async with self.open_source(_observation()) as source:
            # then
            self.assertFalse(await source.unchanged())

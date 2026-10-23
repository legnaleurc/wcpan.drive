"""Synology error translation in SynologyNodeOperations."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.services.operations import OperationUnavailableError
from wcpan.drive.server.services.synology_operations import SynologyNodeOperations
from wcpan.synology import SynologyNetworkError


_RECORD = NodeRecord(
    id=MirrorStableId("f1"),
    parent_id=MirrorStableId("p1"),
    name="f.bin",
    is_directory=False,
    created_time=0,
    modified_time=0,
    changed_time=0,
    mime_type="application/octet-stream",
    hash="",
    hash_algorithm="",
    size=3,
    is_image=False,
    is_video=False,
    width=0,
    height=0,
    ms_duration=0,
)


async def _chunks() -> AsyncGenerator[bytes, None]:
    yield b"abc"


def _drive_api_wrapping_errors() -> MagicMock:
    """A drive API whose download wraps errors like the real client does."""

    @asynccontextmanager
    async def download(*_: Any, **__: Any) -> AsyncGenerator[MagicMock, None]:
        response = MagicMock()
        response.content.iter_any = _chunks
        try:
            yield response
        except Exception as e:
            raise SynologyNetworkError("download failed", e) from e

    drive_api = MagicMock()
    drive_api.download = download
    return drive_api


def _operations(drive_api: MagicMock) -> SynologyNodeOperations:
    return SynologyNodeOperations(
        drive_api=drive_api,
        syno_paths=MagicMock(),
        node_sync=MagicMock(),
    )


class TestDownload(IsolatedAsyncioTestCase):
    async def test_streams_content(self) -> None:
        operations = _operations(_drive_api_wrapping_errors())

        async with operations.download(_RECORD, range_=None) as chunks:
            received = [c async for c in chunks]

        self.assertEqual(received, [b"abc"])

    async def test_client_disconnect_surfaces_as_connection_error(self) -> None:
        operations = _operations(_drive_api_wrapping_errors())

        with self.assertRaises(ConnectionResetError):
            async with operations.download(_RECORD, range_=None):
                raise ConnectionResetError()

    async def test_network_error_becomes_unavailable(self) -> None:
        operations = _operations(_drive_api_wrapping_errors())

        with self.assertRaises(OperationUnavailableError):
            async with operations.download(_RECORD, range_=None):
                raise TimeoutError()

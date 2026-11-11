"""Synology error translation in SynologyNodeOperations."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from aiohttp import ClientResponse
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.services.operations import OperationUnavailableError
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology._operations import SynologyNodeOperations
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.synology import SynologyClient, SynologyNetworkError, SynologyPermanentLink

from ._lib import DecoyTestCase, make_stream


_RECORD = NodeRecord(
    id=NodeId("f1"),
    parent_id=NodeId("p1"),
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


@asynccontextmanager
async def _wrapping_errors(
    response: ClientResponse,
) -> AsyncGenerator[ClientResponse]:
    """Wrap errors like the real client's download does."""
    try:
        yield response
    except Exception as e:
        raise SynologyNetworkError("download failed", e) from e


class TestDownload(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        response = self.decoy.mock(cls=ClientResponse)
        self.decoy.when(response.content).then_return(make_stream(b"abc"))
        drive_api = self.decoy.mock(cls=SynologyClient)
        self.decoy.when(
            drive_api.download(SynologyPermanentLink("f1"), range_=None)
        ).then_return(_wrapping_errors(response))
        self.operations = SynologyNodeOperations(
            drive_api=drive_api,
            syno_paths=self.decoy.mock(cls=SynologyPathService),
            node_sync=self.decoy.mock(cls=NodeSyncService),
        )

    async def test_streams_content(self) -> None:
        # when
        async with self.operations.download(_RECORD, range_=None) as chunks:
            received = [c async for c in chunks]

        # then
        self.assertEqual(received, [b"abc"])

    async def test_client_disconnect_surfaces_as_connection_error(self) -> None:
        # when / then
        with self.assertRaises(ConnectionResetError):
            async with self.operations.download(_RECORD, range_=None):
                raise ConnectionResetError()

    async def test_network_error_becomes_unavailable(self) -> None:
        # when / then
        with self.assertRaises(OperationUnavailableError):
            async with self.operations.download(_RECORD, range_=None):
                raise TimeoutError()

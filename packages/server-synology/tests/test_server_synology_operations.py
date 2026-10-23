"""Synology error translation in SynologyNodeOperations."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from aiohttp import ClientResponse
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._services.operations import OperationUnavailableError
from wcpan.drive.server._services.sync.service import NodeSyncService
from wcpan.drive.server.synology._operations import SynologyNodeOperations
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.synology.exceptions import SynologyNetworkError
from wcpan.synology.types import (
    SynologyClient,
    SynologyPermanentLink,
)

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
)


@asynccontextmanager
async def _responding(response: ClientResponse) -> AsyncGenerator[ClientResponse]:
    """Like the real client: errors from the body pass through unwrapped."""
    yield response


@asynccontextmanager
async def _failing_to_connect() -> AsyncGenerator[ClientResponse]:
    """The real client wraps errors from sending the request."""
    raise SynologyNetworkError("connect failed", ConnectionRefusedError())
    yield  # pragma: no cover


class TestDownload(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        # The public ID is translated to the node's permanent link.
        syno_paths = self.decoy.mock(cls=SynologyPathService)
        self.decoy.when(await syno_paths.permanent_link(NodeId("f1"))).then_return(
            SynologyPermanentLink("link-1")
        )
        self.operations = SynologyNodeOperations(
            drive_api=self.drive_api,
            syno_paths=syno_paths,
            node_sync=self.decoy.mock(cls=NodeSyncService),
        )

    def _respond(
        self, status: int, content_range: str | None, *, body: bytes = b"bc"
    ) -> None:
        response = self.decoy.mock(cls=ClientResponse)
        self.decoy.when(response.status).then_return(status)
        headers = {} if content_range is None else {"Content-Range": content_range}
        self.decoy.when(response.headers).then_return(headers)
        self.decoy.when(response.content).then_return(make_stream(body))
        self.decoy.when(
            self.drive_api.download(SynologyPermanentLink("link-1"), range_=slice(1, 3))
        ).then_return(_responding(response))

    async def test_streams_the_requested_range(self) -> None:
        # given
        self._respond(206, "bytes 1-2/3")

        # when
        async with self.operations.download(_RECORD, offset=1, length=2) as chunks:
            received = [c async for c in chunks]

        # then
        self.assertEqual(received, [b"bc"])

    async def test_range_of_a_changed_file_is_accepted(self) -> None:
        # given: the file grew since it was mirrored; the positions still match
        self._respond(206, "bytes 1-2/9")

        # when
        async with self.operations.download(_RECORD, offset=1, length=2) as chunks:
            received = [c async for c in chunks]

        # then
        self.assertEqual(received, [b"bc"])

    async def test_full_response_is_unavailable(self) -> None:
        # given: a 200, such as an expired session's JSON error
        self._respond(200, None, body=b'{"success":false}')

        # when / then
        with self.assertRaises(OperationUnavailableError):
            async with self.operations.download(_RECORD, offset=1, length=2):
                pass

    async def test_other_range_is_unavailable(self) -> None:
        # given
        self._respond(206, "bytes 0-2/3")

        # when / then
        with self.assertRaises(OperationUnavailableError):
            async with self.operations.download(_RECORD, offset=1, length=2):
                pass

    async def test_network_error_becomes_unavailable(self) -> None:
        # given
        self.decoy.when(
            self.drive_api.download(SynologyPermanentLink("link-1"), range_=slice(1, 3))
        ).then_return(_failing_to_connect())

        # when / then
        with self.assertRaises(OperationUnavailableError):
            async with self.operations.download(_RECORD, offset=1, length=2):
                pass

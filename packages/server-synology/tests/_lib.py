import asyncio
import json
from collections.abc import Mapping, Sequence
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from decoy import Decoy
from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.services.ids import UnknownNodeError, new_node_id
from wcpan.synology import SynologyPath


def mounts_of(paths: Mapping[str, SynologyPath]) -> Mounts:
    """Mounts with the given Synology paths as their sources."""
    return Mounts.from_sources({name: p.path for name, p in paths.items()})


class IdentityIds:
    """``NodeIdMap`` whose public IDs are the permanent links themselves.

    Keeps fixtures readable; ``NodeIds`` is tested in the server package and
    with Synology in ``test_server_node_ids``.
    """

    async def public(self, natives: Sequence[str]) -> list[NodeId]:
        return [NodeId(n) for n in natives]

    async def lookup(self, native: str) -> NodeId | None:
        return NodeId(native)

    async def native_of(self, node_id: NodeId) -> str:
        if not node_id:
            raise UnknownNodeError(node_id)
        return str(node_id)

    def mint(self) -> NodeId:
        return new_node_id()


class DecoyTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)


def make_stream(body: bytes) -> StreamReader:
    """A finished stream holding *body*; needs a running event loop."""
    loop = asyncio.get_running_loop()
    stream = StreamReader(BaseProtocol(loop), 2**16, loop=loop)
    stream.feed_data(body)
    stream.feed_eof()
    return stream


def make_request(
    *,
    app: web.Application | None = None,
    match_info: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    json_body: object = None,
) -> web.Request:
    if json_body is not None:
        body = json.dumps(json_body).encode()
    return make_mocked_request(
        "POST",
        "/",
        headers=headers,
        match_info=match_info or {},
        app=app,
        payload=make_stream(body),
    )

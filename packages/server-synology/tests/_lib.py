import asyncio
import json
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from decoy import Decoy


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

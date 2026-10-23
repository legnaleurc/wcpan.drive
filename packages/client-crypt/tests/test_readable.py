from collections.abc import AsyncIterator
from typing import override

from wcpan.drive.client.crypt._lib import DecryptReadableFile, encrypt
from wcpan.drive.client.types import Node, ReadableFile

from ._lib import DecoyTestCase


class FakeReadableFile(ReadableFile):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    @override
    async def read(self, length: int) -> bytes:
        raise NotImplementedError

    @override
    async def seek(self, offset: int) -> int:
        raise NotImplementedError

    @override
    async def node(self) -> Node:
        raise NotImplementedError


class DecryptReadableFileTestCase(DecoyTestCase):
    async def testIterable(self):
        # given
        content_list = [
            b"xyz",
            b"123",
        ]
        stream = FakeReadableFile([encrypt(content) for content in content_list])

        fin = DecryptReadableFile(stream)
        # when
        chunk_list = [chunk async for chunk in fin]

        # then
        self.assertEqual(chunk_list, content_list)

    async def testRead(self):
        # given
        content = b"789abc"
        mock = self.decoy.mock(cls=ReadableFile)
        self.decoy.when(await mock.read(123)).then_return(encrypt(content))

        fin = DecryptReadableFile(mock)
        # when
        chunk = await fin.read(123)

        # then
        self.assertEqual(content, chunk)

    async def testSeek(self):
        # given
        mock = self.decoy.mock(cls=ReadableFile)
        self.decoy.when(await mock.seek(123)).then_return(123)

        fin = DecryptReadableFile(mock)
        # when / then
        self.assertEqual(await fin.seek(123), 123)

    async def testNode(self):
        # given
        mock = self.decoy.mock(cls=ReadableFile)
        node = self.decoy.mock(cls=Node)
        self.decoy.when(await mock.node()).then_return(node)

        fin = DecryptReadableFile(mock)
        # when / then
        self.assertIs(await fin.node(), node)

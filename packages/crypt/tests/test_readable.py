from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from tests._lib import aexpect
from wcpan.drive.core.types import ReadableFile
from wcpan.drive.crypt._lib import DecryptReadableFile, encrypt


class DecryptReadableFileTestCase(IsolatedAsyncioTestCase):
    async def testIterable(self):
        content_list = [
            b"xyz",
            b"123",
        ]

        async def fake_iterator(self: object):
            for content in content_list:
                yield encrypt(content)

        mock = cast(ReadableFile, AsyncMock(spec=ReadableFile))
        aexpect(mock).__aiter__ = fake_iterator

        fin = DecryptReadableFile(mock)
        chunk_list = [chunk async for chunk in fin]

        self.assertEqual(chunk_list, content_list)

    async def testRead(self):
        content = b"789abc"
        mock = cast(ReadableFile, AsyncMock(spec=ReadableFile))
        aexpect(mock.read).return_value = encrypt(content)

        fin = DecryptReadableFile(mock)
        chunk = await fin.read(123)

        aexpect(mock.read).assert_awaited_once_with(123)
        self.assertEqual(content, chunk)

    async def testSeek(self):
        mock = cast(ReadableFile, AsyncMock(spec=ReadableFile))

        fin = DecryptReadableFile(mock)
        await fin.seek(123)

        aexpect(mock.seek).assert_awaited_once_with(123)

    async def testNode(self):
        mock = cast(ReadableFile, AsyncMock(spec=ReadableFile))

        fin = DecryptReadableFile(mock)
        await fin.node()

        aexpect(mock.node).assert_awaited_once_with()

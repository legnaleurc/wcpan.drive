from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from tests._lib import aexpect, create_amock, create_node
from wcpan.drive.core.types import WritableFile
from wcpan.drive.crypt._lib import EncryptWritableFile, encrypt, encrypt_name


class EncryptWritableFileTestCase(IsolatedAsyncioTestCase):
    async def testTell(self):
        mock = cast(WritableFile, AsyncMock(spec=WritableFile))

        fout = EncryptWritableFile(mock)
        await fout.tell()

        aexpect(mock.tell).assert_awaited_once_with()

    async def testSeek(self):
        mock = cast(WritableFile, AsyncMock(spec=WritableFile))

        fout = EncryptWritableFile(mock)
        await fout.seek(123)

        aexpect(mock.seek).assert_awaited_once_with(123)

    async def testWrite(self):
        content = b"xyz456"
        mock = cast(WritableFile, AsyncMock(spec=WritableFile))

        fout = EncryptWritableFile(mock)
        await fout.write(content)

        content = encrypt(content)
        aexpect(mock.write).assert_awaited_once_with(content)

    async def testNode(self):
        mock = create_amock(WritableFile)
        aexpect(mock.node).return_value = create_node(encrypt_name("name"), None)

        fout = EncryptWritableFile(mock)
        node = await fout.node()

        aexpect(mock.node).assert_awaited_once_with()
        self.assertEqual(node.name, "name")

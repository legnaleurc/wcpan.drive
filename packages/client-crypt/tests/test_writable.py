from wcpan.drive.client.crypt._lib import EncryptWritableFile, encrypt, encrypt_name
from wcpan.drive.client.types import WritableFile

from ._lib import DecoyTestCase, create_node


class EncryptWritableFileTestCase(DecoyTestCase):
    async def testTell(self):
        # given
        mock = self.decoy.mock(cls=WritableFile)
        self.decoy.when(await mock.tell()).then_return(42)

        fout = EncryptWritableFile(mock)
        # when / then
        self.assertEqual(await fout.tell(), 42)

    async def testSeek(self):
        # given
        mock = self.decoy.mock(cls=WritableFile)
        self.decoy.when(await mock.seek(123)).then_return(123)

        fout = EncryptWritableFile(mock)
        # when / then
        self.assertEqual(await fout.seek(123), 123)

    async def testWrite(self):
        # given
        content = b"xyz456"
        mock = self.decoy.mock(cls=WritableFile)
        self.decoy.when(await mock.write(encrypt(content))).then_return(len(content))

        fout = EncryptWritableFile(mock)
        # when / then
        self.assertEqual(await fout.write(content), len(content))

    async def testNode(self):
        # given
        mock = self.decoy.mock(cls=WritableFile)
        self.decoy.when(await mock.node()).then_return(
            create_node(encrypt_name("name"), None)
        )

        fout = EncryptWritableFile(mock)
        # when
        node = await fout.node()

        # then
        self.assertEqual(node.name, "name")

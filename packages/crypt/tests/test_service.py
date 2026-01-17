from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from tests._lib import (
    aexpect,
    create_amock,
    create_mock,
    create_node,
    expect,
    fake_create_hasher,
)
from wcpan.drive.core.types import FileService, Node
from wcpan.drive.crypt._lib import (
    DecryptReadableFile,
    EncryptHasher,
    EncryptWritableFile,
    InvalidCryptVersion,
    encrypt_name,
)
from wcpan.drive.crypt._service import CryptFileService


class GetChangesTestCase(IsolatedAsyncioTestCase):
    async def testRemoved(self):
        upstream = AsyncMock()
        fs = CryptFileService(upstream)

        async def fake_fetch_changes(dummy: object):
            yield (
                [
                    (True, 1),
                ],
                "1",
            )

        upstream.get_changes = fake_fetch_changes

        async for changes, _dummy in fs.get_changes("1"):
            # should not touch remove changes
            self.assertEqual(
                changes[0],
                (True, 1),
            )

    async def testPlainNode(self):
        upstream = AsyncMock()
        fs = CryptFileService(upstream)

        plain_node = create_node("name", None)

        async def fake_fetch_changes(dummy: object):
            yield (
                [
                    (False, plain_node),
                ],
                "1",
            )

        upstream.get_changes = fake_fetch_changes

        async for changes, _dummy in fs.get_changes("1"):
            # should not touch normal files
            self.assertEqual(
                changes[0],
                (False, plain_node),
            )

    async def testCryptNode(self):
        upstream = AsyncMock()
        fs = CryptFileService(upstream)

        crypted_node = create_node(
            encrypt_name("name"),
            {
                "crypt": "1",
            },
        )

        async def fake_fetch_changes(dummy: object):
            yield (
                [
                    (False, crypted_node),
                ],
                "1",
            )

        upstream.get_changes = fake_fetch_changes

        async for changes, _dummy in fs.get_changes("1"):
            # should decrypt the name
            node = cast(Node, changes[0][1])
            self.assertEqual(node.name, "name")

    async def testInvalid(self):
        upstream = AsyncMock()
        fs = CryptFileService(upstream)

        async def fake_fetch_changes(dummy: object):
            yield (
                [
                    (False, create_node("name", {"crypt": "-1"})),
                ],
                "1",
            )

        upstream.get_changes = fake_fetch_changes

        # should not accept invalid crypt version
        with self.assertRaises(InvalidCryptVersion):
            async for _changes, _dummy in fs.get_changes("1"):
                pass


class MoveTestCase(IsolatedAsyncioTestCase):
    async def testPlain(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not touch normal files
        node = create_node("name_1", None)
        new_parent = create_node("name_2", None)
        await fs.move(node, new_parent=new_parent, new_name="new_name", trashed=None)
        aexpect(upstream.move).assert_awaited_once_with(
            node, new_parent=new_parent, new_name="new_name", trashed=None
        )

    async def testCrypt(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not accept invalid crypt version
        node = create_node(
            "name_1",
            {
                "crypt": "-1",
            },
        )
        new_parent = create_node("name_2", None)
        with self.assertRaises(InvalidCryptVersion):
            await fs.move(
                node, new_parent=new_parent, new_name="new_name", trashed=None
            )

    async def testInvalid(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not accept invalid crypt version
        node = create_node(
            "name_1",
            {
                "crypt": "-1",
            },
        )
        new_parent = create_node("name_2", None)
        with self.assertRaises(InvalidCryptVersion):
            await fs.move(
                node, new_parent=new_parent, new_name="new_name", trashed=None
            )
        aexpect(upstream.move).reset_mock()


class CreateDirectoryTestCase(IsolatedAsyncioTestCase):
    async def testInvalid(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not accept invalid crypt version
        node = create_node("name", None)
        with self.assertRaises(InvalidCryptVersion):
            await fs.create_directory(
                "new_name",
                node,
                exist_ok=False,
                private={
                    "crypt": "-1",
                },
            )

    async def testCrypt(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should create encrypted file by default
        node = create_node("name", None)
        await fs.create_directory(
            "new_name",
            node,
            exist_ok=False,
            private=None,
        )
        new_name = encrypt_name("new_name")
        aexpect(upstream.create_directory).assert_awaited_once_with(
            name=new_name,
            parent=node,
            private={
                "crypt": "1",
            },
            exist_ok=False,
        )


class DownloadTestCase(IsolatedAsyncioTestCase):
    async def testInvalid(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not accept invalid crypt version
        node = create_node(
            "name",
            {
                "crypt": "-1",
            },
        )
        with self.assertRaises(InvalidCryptVersion):
            async with fs.download_file(node):
                pass

    async def testPlain(self):
        upstream = create_mock(FileService)
        fs = CryptFileService(upstream)

        expect(upstream.download_file).return_value.__aenter__.return_value = 42
        expect(upstream.download_file).return_value.__aexit__.return_value = None

        # should not touch normal file
        node = create_node("name", None)
        async with fs.download_file(node) as rv:
            expect(upstream.download_file).assert_called_once_with(node)
            self.assertNotIsInstance(rv, DecryptReadableFile)

    async def testCrypt(self):
        upstream = create_mock(FileService)
        fs = CryptFileService(upstream)

        expect(upstream.download_file).return_value.__aenter__.return_value = 42
        expect(upstream.download_file).return_value.__aexit__.return_value = None

        # should create decrypt stream
        node = create_node(
            "name",
            {
                "crypt": "1",
            },
        )
        async with fs.download_file(node) as rv:
            expect(upstream.download_file).assert_called_once_with(node)
            self.assertIsInstance(rv, DecryptReadableFile)


class UploadTestCase(IsolatedAsyncioTestCase):
    async def testInvalid(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # should not accept invalid crypt version
        node = create_node("name", None)
        with self.assertRaises(InvalidCryptVersion):
            async with fs.upload_file(
                parent=node,
                name="new_name",
                size=None,
                mime_type=None,
                media_info=None,
                private={
                    "crypt": "-1",
                },
            ):
                pass

    async def testCrypt(self):
        upstream = create_mock(FileService)
        fs = CryptFileService(upstream)

        expect(upstream.upload_file).return_value.__aenter__.return_value = 42
        expect(upstream.upload_file).return_value.__aexit__.return_value = None

        # should create encrypted file by default
        node = create_node("name", None)
        async with fs.upload_file(
            "new_name",
            node,
            size=None,
            mime_type=None,
            media_info=None,
            private={
                "crypt": "1",
            },
        ) as rv:
            pass
            new_name = encrypt_name("new_name")
            expect(upstream.upload_file).assert_called_once_with(
                new_name,
                node,
                size=None,
                mime_type=None,
                media_info=None,
                private={
                    "crypt": "1",
                },
            )
            self.assertIsInstance(rv, EncryptWritableFile)


class SimpleTestCase(IsolatedAsyncioTestCase):
    async def testGetHahserFactory(self):
        import pickle

        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        # upstream should be pickleable
        aexpect(upstream.get_hasher_factory).return_value = fake_create_hasher

        # should just create the encrypted hasher
        fn = await fs.get_hasher_factory()
        aexpect(upstream.get_hasher_factory).assert_awaited_once_with()

        # should be pickleable
        jar = pickle.dumps(fn)
        new_fn = pickle.loads(jar)

        hasher = await new_fn()
        self.assertIsInstance(hasher, EncryptHasher)

    async def testIsAuthorized(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)
        aexpect(upstream.is_authorized).return_value = False

        rv = await fs.is_authorized()
        aexpect(upstream.is_authorized).assert_awaited_once_with()
        self.assertFalse(rv)

    async def testGetOauthUrl(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)
        aexpect(upstream.get_oauth_url).return_value = "__URL__"

        rv = await fs.get_oauth_url()
        aexpect(upstream.get_oauth_url).assert_awaited_once_with()
        self.assertEqual(rv, "__URL__")

    async def testSetOauthToken(self):
        upstream = create_amock(FileService)
        fs = CryptFileService(upstream)

        await fs.set_oauth_token("__TOKEN__")
        aexpect(upstream.set_oauth_token).assert_awaited_once_with("__TOKEN__")

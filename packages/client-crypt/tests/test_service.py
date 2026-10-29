import pickle
from collections.abc import AsyncIterator
from typing import cast

from decoy import matchers
from wcpan.drive.client.crypt._lib import (
    DecryptReadableFile,
    EncryptHasher,
    EncryptWritableFile,
    InvalidCryptVersion,
    encrypt_name,
)
from wcpan.drive.client.crypt._service import CryptBackend
from wcpan.drive.client.types import (
    Backend,
    ChangeAction,
    Node,
    ReadableFile,
    WritableFile,
)

from ._lib import DecoyTestCase, create_node, fake_create_hasher


async def fake_changes(
    *pages: list[ChangeAction],
) -> AsyncIterator[tuple[list[ChangeAction], str]]:
    for page in pages:
        yield page, "1"


class _TestCase(DecoyTestCase):
    def setUp(self):
        super().setUp()
        self.upstream = self.decoy.mock(cls=Backend)
        self.fs = CryptBackend(self.upstream)


class GetChangesTestCase(_TestCase):
    async def testRemoved(self):
        self.decoy.when(self.upstream.get_changes("1")).then_return(
            fake_changes([(True, "1")])
        )

        async for changes, _dummy in self.fs.get_changes("1"):
            # should not touch remove changes
            self.assertEqual(
                changes[0],
                (True, "1"),
            )

    async def testPlainNode(self):
        plain_node = create_node("name", None)
        self.decoy.when(self.upstream.get_changes("1")).then_return(
            fake_changes([(False, plain_node)])
        )

        async for changes, _dummy in self.fs.get_changes("1"):
            # should not touch normal files
            self.assertEqual(
                changes[0],
                (False, plain_node),
            )

    async def testCryptNode(self):
        crypted_node = create_node(
            encrypt_name("name"),
            {
                "crypt": "1",
            },
        )
        self.decoy.when(self.upstream.get_changes("1")).then_return(
            fake_changes([(False, crypted_node)])
        )

        async for changes, _dummy in self.fs.get_changes("1"):
            # should decrypt the name
            node = cast(Node, changes[0][1])
            self.assertEqual(node.name, "name")

    async def testInvalid(self):
        self.decoy.when(self.upstream.get_changes("1")).then_return(
            fake_changes([(False, create_node("name", {"crypt": "-1"}))])
        )

        # should not accept invalid crypt version
        with self.assertRaises(InvalidCryptVersion):
            async for _changes, _dummy in self.fs.get_changes("1"):
                pass


class MoveTestCase(_TestCase):
    async def testPlain(self):
        # should not touch normal files
        node = create_node("name_1", None)
        new_parent = create_node("name_2", None)
        moved = create_node("new_name", None)
        self.decoy.when(
            await self.upstream.move(node, new_parent=new_parent, new_name="new_name")
        ).then_return(moved)

        rv = await self.fs.move(node, new_parent=new_parent, new_name="new_name")
        self.assertIs(rv, moved)

    async def testCrypt(self):
        # should not accept invalid crypt version
        node = create_node(
            "name_1",
            {
                "crypt": "-1",
            },
        )
        new_parent = create_node("name_2", None)
        with self.assertRaises(InvalidCryptVersion):
            await self.fs.move(node, new_parent=new_parent, new_name="new_name")

    async def testInvalid(self):
        # should not accept invalid crypt version
        node = create_node(
            "name_1",
            {
                "crypt": "-1",
            },
        )
        new_parent = create_node("name_2", None)
        with self.assertRaises(InvalidCryptVersion):
            await self.fs.move(node, new_parent=new_parent, new_name="new_name")


class CreateDirectoryTestCase(_TestCase):
    async def testInvalid(self):
        # should not accept invalid crypt version
        node = create_node("name", None)
        with self.assertRaises(InvalidCryptVersion):
            await self.fs.create_directory(
                "new_name",
                node,
                exist_ok=False,
                private={
                    "crypt": "-1",
                },
            )

    async def testCrypt(self):
        # should create encrypted file by default
        node = create_node("name", None)
        created = create_node("created", None)
        self.decoy.when(
            await self.upstream.create_directory(
                name=encrypt_name("new_name"),
                parent=node,
                private=matchers.DictMatching({"crypt": "1"}),
                exist_ok=matchers.Anything(),
            )
        ).then_return(created)

        rv = await self.fs.create_directory(
            "new_name",
            node,
            exist_ok=False,
            private=None,
        )
        self.assertIs(rv, created)


class DownloadTestCase(_TestCase):
    async def testInvalid(self):
        # should not accept invalid crypt version
        node = create_node(
            "name",
            {
                "crypt": "-1",
            },
        )
        with self.assertRaises(InvalidCryptVersion):
            async with self.fs.download_file(node):
                pass

    async def testPlain(self):
        node = create_node("name", None)
        fin = self.decoy.mock(cls=ReadableFile)
        self.decoy.when(self.upstream.download_file(node)).then_enter_with(fin)

        # should not touch normal file
        async with self.fs.download_file(node) as rv:
            self.assertIs(rv, fin)

    async def testCrypt(self):
        node = create_node(
            "name",
            {
                "crypt": "1",
            },
        )
        fin = self.decoy.mock(cls=ReadableFile)
        self.decoy.when(self.upstream.download_file(node)).then_enter_with(fin)

        # should create decrypt stream
        async with self.fs.download_file(node) as rv:
            self.assertIsInstance(rv, DecryptReadableFile)


class UploadTestCase(_TestCase):
    async def testInvalid(self):
        # should not accept invalid crypt version
        node = create_node("name", None)
        with self.assertRaises(InvalidCryptVersion):
            async with self.fs.upload_file(
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
        node = create_node("name", None)
        fout = self.decoy.mock(cls=WritableFile)
        self.decoy.when(
            self.upstream.upload_file(
                encrypt_name("new_name"),
                node,
                size=matchers.AnythingOrNone(),
                mime_type=matchers.AnythingOrNone(),
                media_info=matchers.AnythingOrNone(),
                private=matchers.DictMatching({"crypt": "1"}),
            )
        ).then_enter_with(fout)

        # should create encrypted file by default
        async with self.fs.upload_file(
            "new_name",
            node,
            size=None,
            mime_type=None,
            media_info=None,
            private={
                "crypt": "1",
            },
        ) as rv:
            self.assertIsInstance(rv, EncryptWritableFile)


class SimpleTestCase(_TestCase):
    async def testGetHahserFactory(self):
        # should forward the node and create the encrypted hasher
        node = create_node("file", {"crypt": "1"})
        self.decoy.when(await self.upstream.get_hasher_factory(node)).then_return(
            fake_create_hasher
        )

        fn = await self.fs.get_hasher_factory(node)

        # should be pickleable
        jar = pickle.dumps(fn)
        new_fn = pickle.loads(jar)

        hasher = await new_fn()
        self.assertIsInstance(hasher, EncryptHasher)

    async def testIsAuthenticated(self):
        self.decoy.when(await self.upstream.is_authenticated()).then_return(False)

        rv = await self.fs.is_authenticated()
        self.assertFalse(rv)

    async def testAuthenticate(self):
        await self.fs.authenticate()
        self.decoy.verify(await self.upstream.authenticate())

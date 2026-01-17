from collections.abc import AsyncIterable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from wcpan.drive.core._drive import create_drive
from wcpan.drive.core.exceptions import (
    NodeExistsError,
    NodeNotFoundError,
    UnauthorizedError,
)
from wcpan.drive.core.types import (
    ChangeAction,
    FileService,
    Node,
    ReadableFile,
    SnapshotService,
    WritableFile,
)


class CreateDriveTestCase(IsolatedAsyncioTestCase):
    async def testCreate(self):
        file_service = Mock(spec=FileService)
        file_service.api_version = 4
        create_file_service = create_mocked_acm(file_service)

        create_file_service_middleware_1 = create_mocked_acm(file_service)
        create_file_service_middleware_2 = create_mocked_acm(file_service)

        snapshot_service = Mock(spec=SnapshotService)
        snapshot_service.api_version = 4
        create_snapshot_service = create_mocked_acm(snapshot_service)

        create_snapshot_service_middleware_1 = create_mocked_acm(snapshot_service)
        create_snapshot_service_middleware_2 = create_mocked_acm(snapshot_service)

        async with create_drive(
            file=create_file_service,
            snapshot=create_snapshot_service,
            file_middleware=[
                create_file_service_middleware_1,
                create_file_service_middleware_2,
            ],
            snapshot_middleware=[
                create_snapshot_service_middleware_1,
                create_snapshot_service_middleware_2,
            ],
        ):
            create_file_service.assert_called_once()
            create_file_service_middleware_1.assert_called_once_with(file_service)
            create_file_service_middleware_2.assert_called_once_with(file_service)
            create_snapshot_service.assert_called_once()
            create_snapshot_service_middleware_1.assert_called_once_with(
                snapshot_service
            )
            create_snapshot_service_middleware_2.assert_called_once_with(
                snapshot_service
            )


class AuthTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testGetOauthUrl(self):
        aexpect(self._fs.get_oauth_url).return_value = 42
        rv = await self._drive.get_oauth_url()

        self.assertEqual(rv, 42)

    async def testIsAuthorized(self):
        aexpect(self._fs.is_authorized).return_value = True
        rv = await self._drive.is_authorized()

        self.assertTrue(rv)

    async def testSetOauthToken(self):
        await self._drive.set_oauth_token("42")

        aexpect(self._fs.set_oauth_token).assert_awaited_once_with("42")


class GetHasherTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testGetHasherFactory(self):
        aexpect(self._fs.get_hasher_factory).return_value = 42
        rv = await self._drive.get_hasher_factory()

        self.assertEqual(rv, 42)


class SnapshotTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testGetRoot(self):
        aexpect(self._ss.get_root).return_value = 42
        rv = await self._drive.get_root()

        self.assertEqual(rv, 42)

    async def testGetNodeById(self):
        aexpect(self._ss.get_node_by_id).return_value = 42
        rv = await self._drive.get_node_by_id("42")

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_node_by_id).assert_awaited_once_with("42")

    async def testGetNodeByPath(self):
        aexpect(self._ss.get_node_by_path).return_value = 42
        path = PurePath("/a/b/c")
        rv = await self._drive.get_node_by_path(path)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_node_by_path).assert_awaited_once_with(path)

    async def testGetChildByName(self):
        aexpect(self._ss.get_child_by_name).return_value = 42
        parent = Mock(spec=Node)
        parent.id = "456"
        rv = await self._drive.get_child_by_name("123", parent)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_child_by_name).assert_awaited_once_with("123", "456")

    async def testGetChildrenById(self):
        aexpect(self._ss.get_children_by_id).return_value = 42
        parent = Mock(spec=Node)
        parent.id = "123"
        rv = await self._drive.get_children(parent)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_children_by_id).assert_awaited_once_with("123")

    async def testGetTrashedNodes(self):
        aexpect(self._ss.get_trashed_nodes).return_value = []
        rv = await self._drive.get_trashed_nodes()

        self.assertEqual(rv, [])
        aexpect(self._ss.get_trashed_nodes).assert_awaited_once_with()

    async def testResolvePath(self):
        path = Path("")
        aexpect(self._ss.resolve_path_by_id).return_value = path
        node = Mock(spec=Node)
        node.id = "123"
        rv = await self._drive.resolve_path(node)

        self.assertEqual(rv, path)
        aexpect(self._ss.resolve_path_by_id).assert_awaited_once_with("123")

    async def testFindNodesByRegex(self):
        aexpect(self._ss.find_nodes_by_regex).return_value = []
        rv = await self._drive.find_nodes_by_regex("123")

        self.assertEqual(rv, [])
        aexpect(self._ss.find_nodes_by_regex).assert_awaited_once_with("123")


class WalkTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testNotFolder(self):
        node = Mock(spec=Node)
        node.is_directory = False

        async for _r, _d, _f in self._drive.walk(node):
            pass

        aexpect(self._ss.get_children_by_id).assert_not_awaited()

    async def testSuccess(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_directory = True
        node.is_trashed = False
        directory = Mock(spec=Node)
        directory.id = "456"
        directory.is_directory = True
        directory.is_trashed = False
        file = Mock(spec=Node)
        file.id = "789"
        file.is_directory = False
        file.is_trashed = False
        aexpect(self._ss.get_children_by_id).side_effect = [[directory, file], []]

        rv: list[object] = []
        async for r, d, f in self._drive.walk(node):
            rv.append((r, d, f))

        self.assertEqual(rv, [(node, [directory], [file]), (directory, [], [])])


class MoveTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )
        self._move = aexpect(self._fs.move)

    async def testMoveRootNode(self):
        node = Mock(spec=Node)
        node.is_trashed = True
        node.id = "123"
        aexpect(self._ss.get_root).return_value = node
        new_parent = Mock(spec=Node)
        new_parent.is_trashed = False

        with self.assertRaises(ValueError):
            await self._drive.move(node, new_parent=new_parent, new_name="123")
        self._move.assert_not_awaited()

    async def testUnauthorized(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            await self._drive.move(
                node, new_parent=new_parent, new_name="123", trashed=True
            )

    async def testNoArgs(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False

        with self.assertRaises(ValueError):
            await self._drive.move(node)
        self._move.assert_not_awaited()

    async def testMoveToNewParent(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        new_parent.is_directory = True
        new_parent.is_trashed = False
        self._move.return_value = 42

        with patch("wcpan.drive.core._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent)

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(
            node, new_parent=new_parent, new_name=None, trashed=None
        )

    async def testMoveToNewName(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        self._move.return_value = 42

        rv = await self._drive.move(node, new_name="456")

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(
            node, new_parent=None, new_name="456", trashed=None
        )

    async def testMoveToNewParentAndNewName(self):
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        new_parent.is_directory = True
        new_parent.is_trashed = False
        self._move.return_value = 42

        with patch("wcpan.drive.core._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent, new_name="789")

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(
            node, new_parent=new_parent, new_name="789", trashed=None
        )

    async def testTrash(self):
        node = Mock(spec=Node)
        node.id = "123"

        await self._drive.move(node, trashed=True)
        aexpect(self._fs.move).assert_awaited_once_with(
            node, new_parent=None, new_name=None, trashed=True
        )


class PurgeTrashTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self):
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            await self._drive.purge_trash()

    async def testSuccess(self):
        await self._drive.purge_trash()
        aexpect(self._fs.purge_trash).assert_awaited_once_with()


class DeleteTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self):
        node = Mock(spec=Node)
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            await self._drive.delete(node)

    async def testSuccess(self):
        node = Mock(spec=Node)

        await self._drive.delete(node)
        aexpect(self._fs.delete).assert_awaited_once_with(node)


class CreateDirectoryTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testInvalidParent(self):
        parent = Mock(spec=Node)
        parent.is_directory = False

        with self.assertRaises(ValueError):
            await self._drive.create_directory("123", parent)

    async def testInvalidName(self):
        parent = Mock(spec=Node)
        parent.is_directory = True

        with self.assertRaises(ValueError):
            await self._drive.create_directory("", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a/b", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a\\b", parent)

    async def testUnauthorized(self):
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            await self._drive.create_directory("123", parent)

    async def testConflicted(self):
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        node = Mock(spec=Node)
        node.id = "456"
        node.name = "aaa"
        aexpect(self._ss.get_child_by_name).return_value = node

        with self.assertRaises(NodeExistsError):
            await self._drive.create_directory("123", parent)

    async def testSuccess(self):
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        aexpect(self._ss.get_child_by_name).side_effect = NodeNotFoundError("")
        node = Mock(spec=Node)
        node.id = "456"
        aexpect(self._fs.create_directory).return_value = node

        rv = await self._drive.create_directory("123", parent)

        self.assertEqual(rv, node)
        aexpect(self._fs.create_directory).assert_awaited_once_with(
            "123", parent, exist_ok=False, private=None
        )


class DownloadFileTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testNotFile(self):
        node = Mock(spec=Node)
        node.is_directory = True

        with self.assertRaises(ValueError):
            async with self._drive.download_file(node):
                pass

    async def testUnauthorized(self):
        node = Mock(spec=Node)
        node.is_directory = False
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            async with self._drive.download_file(node):
                pass

    async def testSuccess(self):
        node = Mock(spec=Node)
        node.is_directory = False
        aexpect(self._fs.is_authorized).return_value = True
        fin = Mock(spec=ReadableFile)
        aexpect(self._fs.download_file).return_value.__aenter__.return_value = fin

        async with self._drive.download_file(node) as rv:
            self.assertEqual(rv, fin)

        aexpect(self._fs.download_file).assert_called_once_with(node)


class UploadFileTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self):
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testNotFolder(self):
        parent = Mock(spec=Node)
        parent.is_directory = False
        aexpect(self._fs.is_authorized).return_value = True

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testInvalidName(self):
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authorized).return_value = True

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("", parent):
                pass

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("a/b", parent):
                pass

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("a\\b", parent):
                pass

    async def testConflicted(self):
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        node = Mock(spec=Node)
        node.name = "456"
        aexpect(self._ss.get_child_by_name).return_value = node

        with self.assertRaises(NodeExistsError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testSuccess(self):
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        aexpect(self._fs.is_authorized).return_value = True
        aexpect(self._ss.get_child_by_name).side_effect = NodeNotFoundError("123")
        fout = Mock(spec=WritableFile)
        aexpect(self._fs.upload_file).return_value.__aenter__.return_value = fout

        async with self._drive.upload_file(
            "123", parent, size=123, mime_type="text/plain"
        ) as rv:
            self.assertEqual(rv, fout)

        aexpect(self._fs.upload_file).assert_called_once_with(
            "123",
            parent,
            size=123,
            mime_type="text/plain",
            media_info=None,
            private=None,
        )


class SyncTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self):
        aexpect(self._fs.is_authorized).return_value = False

        with self.assertRaises(UnauthorizedError):
            async for _ in self._drive.sync():
                pass

    async def testResetRoot(self):
        aexpect(self._fs.get_initial_cursor).return_value = "123"
        aexpect(self._ss.get_current_cursor).return_value = ""
        node = Mock(spec=Node)
        aexpect(self._fs.get_root).return_value = node
        changes = []
        aexpect(self._fs.get_changes).return_value = to_async_iterable(changes)
        async for _ in self._drive.sync():
            pass

        aexpect(self._ss.set_root).assert_awaited_once_with(node)

    async def testApply(self):
        aexpect(self._fs.get_initial_cursor).return_value = "123"
        aexpect(self._ss.get_current_cursor).return_value = "456"
        changes = [
            ([(True, "123")], "789"),
        ]
        aexpect(self._fs.get_changes).return_value = to_async_iterable(changes)
        rv: list[ChangeAction] = []
        async for _ in self._drive.sync():
            rv.append(_)

        self.assertEqual(rv, [(True, "123")])
        aexpect(self._ss.apply_changes).assert_called_once_with([(True, "123")], "789")


def create_mocked_acm(rv: Mock) -> Mock:
    acm = MagicMock()
    acm.return_value.__aenter__.return_value = rv
    acm.return_value.__aexit__.return_value = None
    return acm


async def to_async_iterable[T](rv: Iterable[T]) -> AsyncIterable[T]:
    for _ in rv:
        yield _


@asynccontextmanager
async def create_mocked_drive():
    file_service = MagicMock(spec=FileService)
    file_service.api_version = 4
    create_file_service = create_mocked_acm(file_service)

    snapshot_service = MagicMock(spec=SnapshotService)
    snapshot_service.api_version = 4
    create_snapshot_service = create_mocked_acm(snapshot_service)

    async with create_drive(
        file=create_file_service, snapshot=create_snapshot_service
    ) as drive:
        yield (
            drive,
            cast(FileService, file_service),
            cast(SnapshotService, snapshot_service),
        )


def aexpect(unknown: object) -> AsyncMock:
    return cast(AsyncMock, unknown)

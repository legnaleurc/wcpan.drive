from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from wcpan.drive.client._drive import (
    _VIRTUAL_ROOT_ID,
    compose_service,
    create_drive,
    create_multi_drive,
)
from wcpan.drive.client.exceptions import (
    AuthenticationError,
    NodeExistsError,
    NodeNotFoundError,
)
from wcpan.drive.client.lib import dispatch_change
from wcpan.drive.client.types import (
    Backend,
    Cache,
    ChangeAction,
    Node,
    ReadableFile,
    SourceConfig,
    UpdateAction,
    WritableFile,
)


def make_node(
    *,
    node_id: str,
    parent_id: str | None = None,
    name: str = "",
    is_directory: bool = False,
    is_trashed: bool = False,
) -> Node:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return Node(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        is_trashed=is_trashed,
        created_time=epoch,
        modified_time=epoch,
        changed_time=epoch,
        mime_type="",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )


class CreateDriveTestCase(IsolatedAsyncioTestCase):
    async def testCreate(self) -> None:
        backend = Mock(spec=Backend)
        backend.api_version = 6
        create_backend = create_mocked_acm(backend)

        create_backend_middleware_1 = create_mocked_acm(backend)
        create_backend_middleware_2 = create_mocked_acm(backend)

        cache = Mock(spec=Cache)
        cache.api_version = 6
        create_cache = create_mocked_acm(cache)

        create_cache_middleware_1 = create_mocked_acm(cache)
        create_cache_middleware_2 = create_mocked_acm(cache)

        async with create_drive(
            backend=compose_service(
                create_backend,
                create_backend_middleware_1,
                create_backend_middleware_2,
            ),
            cache=compose_service(
                create_cache,
                create_cache_middleware_1,
                create_cache_middleware_2,
            ),
        ):
            create_backend.assert_called_once()
            create_backend_middleware_1.assert_called_once_with(backend)
            create_backend_middleware_2.assert_called_once_with(backend)
            create_cache.assert_called_once()
            create_cache_middleware_1.assert_called_once_with(cache)
            create_cache_middleware_2.assert_called_once_with(cache)

    async def testMultiMode(self) -> None:
        backend_1 = Mock(spec=Backend)
        backend_1.api_version = 6
        backend_2 = Mock(spec=Backend)
        backend_2.api_version = 6

        cache_1 = Mock(spec=Cache)
        cache_1.api_version = 6
        cache_2 = Mock(spec=Cache)
        cache_2.api_version = 6

        async with create_multi_drive(
            sources=[
                SourceConfig(
                    name="google",
                    backend=create_mocked_acm(backend_1),
                    cache=create_mocked_acm(cache_1),
                ),
                SourceConfig(
                    name="local",
                    backend=create_mocked_acm(backend_2),
                    cache=create_mocked_acm(cache_2),
                ),
            ]
        ) as drive:
            root = await drive.get_root()
            self.assertEqual(root.id, _VIRTUAL_ROOT_ID)
            self.assertIsNone(root.parent_id)
            self.assertTrue(root.is_directory)


class AuthTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testIsAuthenticated(self) -> None:
        aexpect(self._fs.is_authenticated).return_value = True
        rv = await self._drive.is_authenticated()

        self.assertTrue(rv)

    async def testAuthenticate(self) -> None:
        await self._drive.authenticate()

        aexpect(self._fs.authenticate).assert_awaited_once_with()


class GetHasherTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testGetHasherFactory(self) -> None:
        node = make_node(node_id="abc")
        aexpect(self._fs.get_hasher_factory).return_value = 42
        rv = await self._drive.get_hasher_factory(node)

        self.assertEqual(rv, 42)
        aexpect(self._fs.get_hasher_factory).assert_awaited_once_with(node)


class CacheTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testGetRoot(self) -> None:
        aexpect(self._ss.get_root).return_value = 42
        rv = await self._drive.get_root()

        self.assertEqual(rv, 42)

    async def testGetNodeById(self) -> None:
        aexpect(self._ss.get_node_by_id).return_value = 42
        rv = await self._drive.get_node_by_id("42")

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_node_by_id).assert_awaited_once_with("42")

    async def testGetNodeByPath(self) -> None:
        aexpect(self._ss.get_node_by_path).return_value = 42
        path = PurePath("/a/b/c")
        rv = await self._drive.get_node_by_path(path)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_node_by_path).assert_awaited_once_with(path)

    async def testGetChildByName(self) -> None:
        aexpect(self._ss.get_child_by_name).return_value = 42
        parent = Mock(spec=Node)
        parent.id = "456"
        rv = await self._drive.get_child_by_name("123", parent)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_child_by_name).assert_awaited_once_with("123", "456")

    async def testGetChildrenById(self) -> None:
        aexpect(self._ss.get_children_by_id).return_value = 42
        parent = Mock(spec=Node)
        parent.id = "123"
        rv = await self._drive.get_children(parent)

        self.assertEqual(rv, 42)
        aexpect(self._ss.get_children_by_id).assert_awaited_once_with("123")

    async def testGetTrashedNodes(self) -> None:
        aexpect(self._ss.get_trashed_nodes).return_value = []
        rv = await self._drive.get_trashed_nodes()

        self.assertEqual(rv, [])
        aexpect(self._ss.get_trashed_nodes).assert_awaited_once_with()

    async def testGetTrashedNodesFlatten(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        aexpect(self._ss.get_trashed_nodes).return_value = [trashed_dir, child]

        rv = await self._drive.get_trashed_nodes(flatten=True)

        self.assertEqual(rv, [trashed_dir, child])

    async def testGetTrashedNodesFiltered(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        aexpect(self._ss.get_trashed_nodes).return_value = [trashed_dir, child]

        rv = await self._drive.get_trashed_nodes(flatten=False)

        self.assertEqual(rv, [trashed_dir])

    async def testResolvePath(self) -> None:
        path = Path("")
        aexpect(self._ss.resolve_path_by_id).return_value = path
        node = Mock(spec=Node)
        node.id = "123"
        rv = await self._drive.resolve_path(node)

        self.assertEqual(rv, path)
        aexpect(self._ss.resolve_path_by_id).assert_awaited_once_with("123")

    async def testFindNodesByRegex(self) -> None:
        aexpect(self._ss.find_nodes_by_regex).return_value = []
        rv = await self._drive.find_nodes_by_regex("123")

        self.assertEqual(rv, [])
        aexpect(self._ss.find_nodes_by_regex).assert_awaited_once_with("123")


class WalkTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testNotFolder(self) -> None:
        node = Mock(spec=Node)
        node.is_directory = False

        async for _r, _d, _f in self._drive.walk(node):
            pass

        aexpect(self._ss.get_children_by_id).assert_not_awaited()

    async def testSuccess(self) -> None:
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

    async def testMoveRootNode(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        aexpect(self._ss.get_root).return_value = node
        new_parent = Mock(spec=Node)

        with self.assertRaises(ValueError):
            await self._drive.move(node, new_parent=new_parent, new_name="123")
        self._move.assert_not_awaited()

    async def testUnauthorized(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            await self._drive.move(node, new_parent=new_parent, new_name="123")

    async def testNoArgs(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False

        with self.assertRaises(ValueError):
            await self._drive.move(node)
        self._move.assert_not_awaited()

    async def testMoveToNewParent(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        new_parent.is_directory = True
        new_parent.is_trashed = False
        self._move.return_value = 42

        with patch("wcpan.drive.client._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent)

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(node, new_parent=new_parent, new_name=None)

    async def testMoveToNewName(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        self._move.return_value = 42

        rv = await self._drive.move(node, new_name="456")

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(node, new_parent=None, new_name="456")

    async def testMoveToNewParentAndNewName(self) -> None:
        node = Mock(spec=Node)
        node.id = "123"
        node.is_trashed = False
        new_parent = Mock(spec=Node)
        new_parent.is_directory = True
        new_parent.is_trashed = False
        self._move.return_value = 42

        with patch("wcpan.drive.client._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent, new_name="789")

        self.assertEqual(rv, 42)
        self._move.assert_awaited_once_with(node, new_parent=new_parent, new_name="789")


class PurgeTrashTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self) -> None:
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            await self._drive.purge_trash()

    async def testSuccess(self) -> None:
        await self._drive.purge_trash()
        aexpect(self._fs.purge_trash).assert_awaited_once_with()


class DeleteTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self) -> None:
        node = Mock(spec=Node)
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            await self._drive.delete(node)

    async def testSuccess(self) -> None:
        node = Mock(spec=Node)

        await self._drive.delete(node)
        aexpect(self._fs.delete).assert_awaited_once_with(node, permanent=False)

    async def testPermanent(self) -> None:
        node = Mock(spec=Node)

        await self._drive.delete(node, permanent=True)
        aexpect(self._fs.delete).assert_awaited_once_with(node, permanent=True)


class RestoreTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testUnauthorized(self) -> None:
        node = Mock(spec=Node)
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            await self._drive.restore(node)

    async def testSuccess(self) -> None:
        node = Mock(spec=Node)
        restored = Mock(spec=Node)
        aexpect(self._fs.restore).return_value = restored

        rv = await self._drive.restore(node)

        self.assertEqual(rv, restored)
        aexpect(self._fs.restore).assert_awaited_once_with(node)


class CreateDirectoryTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._drive, self._fs, self._ss = await self.enterAsyncContext(
            create_mocked_drive()
        )

    async def testInvalidParent(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = False

        with self.assertRaises(ValueError):
            await self._drive.create_directory("123", parent)

    async def testInvalidName(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = True

        with self.assertRaises(ValueError):
            await self._drive.create_directory("", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a/b", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a\\b", parent)

    async def testUnauthorized(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            await self._drive.create_directory("123", parent)

    async def testConflicted(self) -> None:
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        node = Mock(spec=Node)
        node.id = "456"
        node.name = "aaa"
        aexpect(self._ss.get_child_by_name).return_value = node

        with self.assertRaises(NodeExistsError):
            await self._drive.create_directory("123", parent)

    async def testSuccess(self) -> None:
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

    async def testNotFile(self) -> None:
        node = Mock(spec=Node)
        node.is_directory = True

        with self.assertRaises(ValueError):
            async with self._drive.download_file(node):
                pass

    async def testUnauthorized(self) -> None:
        node = Mock(spec=Node)
        node.is_directory = False
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            async with self._drive.download_file(node):
                pass

    async def testSuccess(self) -> None:
        node = Mock(spec=Node)
        node.is_directory = False
        aexpect(self._fs.is_authenticated).return_value = True
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

    async def testUnauthorized(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testNotFolder(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = False
        aexpect(self._fs.is_authenticated).return_value = True

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testInvalidName(self) -> None:
        parent = Mock(spec=Node)
        parent.is_directory = True
        aexpect(self._fs.is_authenticated).return_value = True

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("", parent):
                pass

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("a/b", parent):
                pass

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("a\\b", parent):
                pass

    async def testConflicted(self) -> None:
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        node = Mock(spec=Node)
        node.name = "456"
        aexpect(self._ss.get_child_by_name).return_value = node

        with self.assertRaises(NodeExistsError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testSuccess(self) -> None:
        parent = Mock(spec=Node)
        parent.id = "123"
        parent.is_directory = True
        aexpect(self._fs.is_authenticated).return_value = True
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

    async def testUnauthorized(self) -> None:
        aexpect(self._fs.is_authenticated).return_value = False

        with self.assertRaises(AuthenticationError):
            async for _ in self._drive.sync():
                pass

    async def testResetRoot(self) -> None:
        aexpect(self._fs.get_initial_cursor).return_value = "123"
        aexpect(self._ss.get_current_cursor).return_value = ""
        node = Mock(spec=Node)
        aexpect(self._fs.get_root).return_value = node
        changes: list[ChangeAction] = []
        aexpect(self._fs.get_changes).return_value = to_async_iterable(changes)
        async for _ in self._drive.sync():
            pass

        aexpect(self._ss.set_root).assert_awaited_once_with(node)

    async def testApply(self) -> None:
        aexpect(self._fs.get_initial_cursor).return_value = "123"
        aexpect(self._ss.get_current_cursor).return_value = "456"
        actions: list[ChangeAction] = [(True, "123")]
        changes = [(actions, "789")]
        aexpect(self._fs.get_changes).return_value = to_async_iterable(changes)
        rv: list[ChangeAction] = []
        async for _ in self._drive.sync():
            rv.append(_)

        self.assertEqual(rv, [(True, "123")])
        aexpect(self._ss.apply_changes).assert_called_once_with([(True, "123")], "789")


# --- Multi-source (_MultiDrive) tests ---


class MultiDriveGetRootTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetRoot(self) -> None:
        root = await self._drive.get_root()
        self.assertEqual(root.id, _VIRTUAL_ROOT_ID)
        self.assertIsNone(root.parent_id)
        self.assertTrue(root.is_directory)
        self.assertFalse(root.is_trashed)


class MultiDriveGetChildrenTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetChildrenOfRoot(self) -> None:
        google_root = make_node(node_id="groot", name="", is_directory=True)
        local_root = make_node(node_id="lroot", name="", is_directory=True)
        aexpect(self._sources["google"].cache.get_root).return_value = google_root
        aexpect(self._sources["local"].cache.get_root).return_value = local_root

        virtual_root = await self._drive.get_root()
        children = await self._drive.get_children(virtual_root)

        self.assertEqual(len(children), 2)
        # Children should have scoped IDs and parent_id == virtual root
        ids = {c.id for c in children}
        self.assertIn("google:groot", ids)
        self.assertIn("local:lroot", ids)
        names = {c.name for c in children}
        self.assertIn("google", names)
        self.assertIn("local", names)
        for child in children:
            self.assertEqual(child.parent_id, _VIRTUAL_ROOT_ID)

    async def testGetChildrenOfSourceRoot(self) -> None:
        backend_child = make_node(
            node_id="child1", parent_id="groot", name="docs", is_directory=True
        )
        aexpect(self._sources["google"].cache.get_children_by_id).return_value = [
            backend_child
        ]

        source_root = make_node(
            node_id="google:groot",
            parent_id=_VIRTUAL_ROOT_ID,
            name="google",
            is_directory=True,
        )
        children = await self._drive.get_children(source_root)

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].id, "google:child1")
        self.assertEqual(children[0].parent_id, "google:groot")
        aexpect(
            self._sources["google"].cache.get_children_by_id
        ).assert_awaited_once_with("groot")


class MultiDriveGetChildByNameTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetChildByNameFromVirtualRoot(self) -> None:
        backend_root = make_node(node_id="groot", name="", is_directory=True)
        aexpect(self._sources["google"].cache.get_root).return_value = backend_root

        virtual_root = await self._drive.get_root()
        node = await self._drive.get_child_by_name("google", virtual_root)

        self.assertEqual(node.id, "google:groot")
        self.assertEqual(node.parent_id, _VIRTUAL_ROOT_ID)
        self.assertEqual(node.name, "google")


class MultiDriveGetNodeByPathTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetRootPath(self) -> None:
        root = await self._drive.get_node_by_path(PurePath("/"))
        self.assertEqual(root.id, _VIRTUAL_ROOT_ID)

    async def testGetSourceRootPath(self) -> None:
        backend_root = make_node(node_id="groot", is_directory=True)
        aexpect(self._sources["google"].cache.get_root).return_value = backend_root

        node = await self._drive.get_node_by_path(PurePath("/google"))
        self.assertEqual(node.id, "google:groot")
        self.assertEqual(node.parent_id, _VIRTUAL_ROOT_ID)
        self.assertEqual(node.name, "google")

    async def testGetDeepPath(self) -> None:
        backend_node = make_node(
            node_id="file1", parent_id="docs", name="file.txt", is_directory=False
        )
        aexpect(
            self._sources["google"].cache.get_node_by_path
        ).return_value = backend_node

        node = await self._drive.get_node_by_path(PurePath("/google/docs/file.txt"))
        self.assertEqual(node.id, "google:file1")
        aexpect(
            self._sources["google"].cache.get_node_by_path
        ).assert_awaited_once_with(PurePath("/docs/file.txt"))

    async def testUnknownSource(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            await self._drive.get_node_by_path(PurePath("/unknown/path"))


class MultiDriveResolvePathTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testResolveVirtualRoot(self) -> None:
        root = await self._drive.get_root()
        path = await self._drive.resolve_path(root)
        self.assertEqual(path, PurePath("/"))

    async def testResolveSourceRoot(self) -> None:
        source_root = make_node(
            node_id="google:groot",
            parent_id=_VIRTUAL_ROOT_ID,
            name="google",
            is_directory=True,
        )
        path = await self._drive.resolve_path(source_root)
        self.assertEqual(path, PurePath("/google"))

    async def testResolveDeepNode(self) -> None:
        aexpect(
            self._sources["google"].cache.resolve_path_by_id
        ).return_value = PurePath("/docs/file.txt")

        node = make_node(
            node_id="google:file1",
            parent_id="google:docs",
            name="file.txt",
            is_directory=False,
        )
        path = await self._drive.resolve_path(node)
        self.assertEqual(path, PurePath("/google/docs/file.txt"))
        aexpect(
            self._sources["google"].cache.resolve_path_by_id
        ).assert_awaited_once_with("file1")


class MultiDriveSyncTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testSyncYieldsScopedChanges(self) -> None:
        google_fs = self._sources["google"].backend
        google_ss = self._sources["google"].cache
        local_fs = self._sources["local"].backend
        local_ss = self._sources["local"].cache

        aexpect(google_fs.is_authenticated).return_value = True
        aexpect(local_fs.is_authenticated).return_value = True

        backend_node = make_node(node_id="file1", parent_id="root1", name="file.txt")
        aexpect(google_fs.get_initial_cursor).return_value = "cur0"
        aexpect(google_ss.get_current_cursor).return_value = "cur1"
        google_actions: list[ChangeAction] = [(False, backend_node), (True, "old_id")]
        aexpect(google_fs.get_changes).return_value = to_async_iterable(
            [(google_actions, "cur2")]
        )

        aexpect(local_fs.get_initial_cursor).return_value = "lcur0"
        aexpect(local_ss.get_current_cursor).return_value = "lcur1"
        aexpect(local_fs.get_changes).return_value = to_async_iterable([])

        rv: list[ChangeAction] = []
        async for change in self._drive.sync():
            rv.append(change)

        self.assertEqual(len(rv), 2)
        # UpdateAction - node should be scoped
        self.assertFalse(rv[0][0])
        update = cast(UpdateAction, rv[0])
        self.assertEqual(update[1].id, "google:file1")
        self.assertEqual(update[1].parent_id, "google:root1")
        # RemoveAction - id should be scoped
        self.assertTrue(rv[1][0])
        self.assertEqual(rv[1][1], "google:old_id")

    async def testSyncUnauthorized(self) -> None:
        aexpect(self._sources["google"].backend.is_authenticated).return_value = False
        aexpect(self._sources["local"].backend.is_authenticated).return_value = True

        with self.assertRaises(AuthenticationError):
            async for _ in self._drive.sync():
                pass

    async def testSyncBothSourcesEmitChanges(self) -> None:
        google_fs = self._sources["google"].backend
        google_ss = self._sources["google"].cache
        local_fs = self._sources["local"].backend
        local_ss = self._sources["local"].cache

        aexpect(google_fs.is_authenticated).return_value = True
        aexpect(local_fs.is_authenticated).return_value = True

        google_node = make_node(node_id="g1", parent_id="groot", name="google_file.txt")
        local_node = make_node(node_id="l1", parent_id="lroot", name="local_file.txt")

        aexpect(google_fs.get_initial_cursor).return_value = "gcur0"
        aexpect(google_ss.get_current_cursor).return_value = "gcur1"
        google_actions: list[ChangeAction] = [(False, google_node), (True, "gremoved")]
        aexpect(google_fs.get_changes).return_value = to_async_iterable(
            [(google_actions, "gcur2")]
        )

        aexpect(local_fs.get_initial_cursor).return_value = "lcur0"
        aexpect(local_ss.get_current_cursor).return_value = "lcur1"
        local_actions: list[ChangeAction] = [(False, local_node), (True, "lremoved")]
        aexpect(local_fs.get_changes).return_value = to_async_iterable(
            [(local_actions, "lcur2")]
        )

        rv: list[ChangeAction] = []
        async for change in self._drive.sync():
            rv.append(change)

        self.assertEqual(len(rv), 4)

        ids = {
            dispatch_change(c, on_remove=lambda s: s, on_update=lambda n: n.id)
            for c in rv
        }
        google_ids = {id for id in ids if id.startswith("google:")}
        local_ids = {id for id in ids if id.startswith("local:")}

        self.assertEqual(len(google_ids), 2)
        self.assertEqual(len(local_ids), 2)
        self.assertIn("google:g1", google_ids)
        self.assertIn("google:gremoved", google_ids)
        self.assertIn("local:l1", local_ids)
        self.assertIn("local:lremoved", local_ids)


class MultiDriveMoveTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testCrossSourceMoveRaises(self) -> None:
        google_node = make_node(
            node_id="google:file1", parent_id="google:root", name="file.txt"
        )
        local_parent = make_node(
            node_id="local:dir1",
            parent_id="local:root",
            name="dir",
            is_directory=True,
        )
        aexpect(self._sources["google"].backend.is_authenticated).return_value = True
        aexpect(self._sources["local"].backend.is_authenticated).return_value = True

        with self.assertRaises(ValueError, msg="cannot move across sources"):
            await self._drive.move(google_node, new_parent=local_parent)

    async def testMoveVirtualRootRaises(self) -> None:
        virtual_root = await self._drive.get_root()
        aexpect(self._sources["google"].backend.is_authenticated).return_value = True
        aexpect(self._sources["local"].backend.is_authenticated).return_value = True

        with self.assertRaises(ValueError):
            await self._drive.move(virtual_root, new_name="newname")

    async def testMoveSourceRootRaises(self) -> None:
        source_root = make_node(
            node_id="google:root",
            parent_id=_VIRTUAL_ROOT_ID,
            name="google",
            is_directory=True,
        )
        aexpect(self._sources["google"].backend.is_authenticated).return_value = True
        aexpect(self._sources["local"].backend.is_authenticated).return_value = True

        with self.assertRaises(ValueError):
            await self._drive.move(source_root, new_name="newname")


class MultiDriveFindNodesTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testFindNodesByRegexMergesResults(self) -> None:
        google_node = make_node(node_id="g1", parent_id="groot", name="photo.jpg")
        local_node = make_node(node_id="l1", parent_id="lroot", name="photo.png")
        aexpect(self._sources["google"].cache.find_nodes_by_regex).return_value = [
            google_node
        ]
        aexpect(self._sources["local"].cache.find_nodes_by_regex).return_value = [
            local_node
        ]

        results = await self._drive.find_nodes_by_regex(r"photo")

        self.assertEqual(len(results), 2)
        ids = {n.id for n in results}
        self.assertIn("google:g1", ids)
        self.assertIn("local:l1", ids)


class MultiDriveGetNodeByIdTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetNodeById(self) -> None:
        backend_node = make_node(node_id="abc", parent_id="groot", name="file.txt")
        aexpect(
            self._sources["google"].cache.get_node_by_id
        ).return_value = backend_node

        result = await self._drive.get_node_by_id("google:abc")

        self.assertEqual(result.id, "google:abc")
        self.assertEqual(result.parent_id, "google:groot")
        aexpect(self._sources["google"].cache.get_node_by_id).assert_awaited_once_with(
            "abc"
        )

    async def testGetNodeByIdUnknownSource(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            await self._drive.get_node_by_id("unknown:abc")


class MultiDriveGetTrashedNodesTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetTrashedNodesEmpty(self) -> None:
        aexpect(self._sources["google"].cache.get_trashed_nodes).return_value = []
        aexpect(self._sources["local"].cache.get_trashed_nodes).return_value = []

        result = await self._drive.get_trashed_nodes()

        self.assertEqual(result, [])

    async def testGetTrashedNodesMerged(self) -> None:
        google_node = make_node(
            node_id="g1", parent_id="groot", name="google_trash.txt", is_trashed=True
        )
        local_node = make_node(
            node_id="l1", parent_id="lroot", name="local_trash.txt", is_trashed=True
        )
        aexpect(self._sources["google"].cache.get_trashed_nodes).return_value = [
            google_node
        ]
        aexpect(self._sources["local"].cache.get_trashed_nodes).return_value = [
            local_node
        ]

        result = await self._drive.get_trashed_nodes()

        ids = {n.id for n in result}
        self.assertIn("google:g1", ids)
        self.assertIn("local:l1", ids)

    async def testGetTrashedNodesFlatten(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        aexpect(self._sources["google"].cache.get_trashed_nodes).return_value = [
            trashed_dir,
            child,
        ]
        aexpect(self._sources["local"].cache.get_trashed_nodes).return_value = []

        result = await self._drive.get_trashed_nodes(flatten=True)

        ids = {n.id for n in result}
        self.assertIn("google:d1", ids)
        self.assertIn("google:f1", ids)

    async def testGetTrashedNodesFiltered(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        aexpect(self._sources["google"].cache.get_trashed_nodes).return_value = [
            trashed_dir,
            child,
        ]
        aexpect(self._sources["local"].cache.get_trashed_nodes).return_value = []

        result = await self._drive.get_trashed_nodes(flatten=False)

        ids = {n.id for n in result}
        self.assertIn("google:d1", ids)
        self.assertNotIn("google:f1", ids)


class MultiDriveGetHasherTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testGetHasherFactory(self) -> None:
        node = make_node(node_id="local:file1", parent_id="local:root", name="file.txt")
        aexpect(self._sources["local"].backend.get_hasher_factory).return_value = 99
        rv = await self._drive.get_hasher_factory(node)

        self.assertEqual(rv, 99)
        unscoped = aexpect(
            self._sources["local"].backend.get_hasher_factory
        ).await_args.args[0]
        self.assertEqual(unscoped.id, "file1")
        self.assertEqual(unscoped.parent_id, "root")
        aexpect(self._sources["google"].backend.get_hasher_factory).assert_not_awaited()

    async def testGetHasherFactoryRoutesToCorrectSource(self) -> None:
        google_node = make_node(node_id="google:g1", parent_id="google:root")
        local_node = make_node(node_id="local:l1", parent_id="local:root")
        aexpect(self._sources["google"].backend.get_hasher_factory).return_value = 1
        aexpect(self._sources["local"].backend.get_hasher_factory).return_value = 2

        rv_google = await self._drive.get_hasher_factory(google_node)
        rv_local = await self._drive.get_hasher_factory(local_node)

        self.assertEqual(rv_google, 1)
        self.assertEqual(rv_local, 2)


class MultiDriveDeleteTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ctx = create_mocked_multi_drive(["google", "local"])
        self._drive, self._sources = await self.enterAsyncContext(ctx)

    async def testUnauthorized(self) -> None:
        aexpect(self._sources["google"].backend.is_authenticated).return_value = False
        aexpect(self._sources["local"].backend.is_authenticated).return_value = True
        node = make_node(
            node_id="google:file1", parent_id="google:root", name="file.txt"
        )

        with self.assertRaises(AuthenticationError):
            await self._drive.delete(node)

    async def testSuccess(self) -> None:
        node = make_node(
            node_id="google:file1", parent_id="google:root", name="file.txt"
        )

        await self._drive.delete(node)

        aexpect(self._sources["google"].backend.delete).assert_awaited_once()
        call_args = aexpect(self._sources["google"].backend.delete).call_args
        deleted_node = call_args[0][0]
        self.assertEqual(deleted_node.id, "file1")
        self.assertEqual(call_args[1]["permanent"], False)


def create_mocked_acm(rv: Mock) -> Mock:
    acm = MagicMock()
    acm.return_value.__aenter__.return_value = rv
    acm.return_value.__aexit__.return_value = None
    return acm


async def to_async_iterable[T](rv: Iterable[T]) -> AsyncGenerator[T, None]:
    for _ in rv:
        yield _


@asynccontextmanager
async def create_mocked_drive() -> AsyncIterator[tuple[Any, Backend, Cache]]:
    backend = MagicMock(spec=Backend)
    backend.api_version = 6
    create_backend = create_mocked_acm(backend)

    cache = MagicMock(spec=Cache)
    cache.api_version = 6
    create_cache = create_mocked_acm(cache)

    async with create_drive(
        backend=create_backend,
        cache=create_cache,
    ) as drive:
        yield (
            drive,
            cast(Backend, backend),
            cast(Cache, cache),
        )


@asynccontextmanager
async def create_mocked_multi_drive(source_names: list[str]) -> AsyncIterator[Any]:
    from wcpan.drive.client._drive import _SourceState

    source_configs: list[SourceConfig] = []
    source_states: dict[str, _SourceState] = {}

    for name in source_names:
        fs = MagicMock(spec=Backend)
        fs.api_version = 6
        # Default: authenticated
        fs.is_authenticated = AsyncMock(return_value=True)

        ss = MagicMock(spec=Cache)
        ss.api_version = 6

        source_configs.append(
            SourceConfig(
                name=name,
                backend=create_mocked_acm(fs),
                cache=create_mocked_acm(ss),
            )
        )
        source_states[name] = _SourceState(backend=fs, cache=ss)

    async with create_multi_drive(sources=source_configs) as drive:
        yield drive, source_states


def aexpect(unknown: object) -> AsyncMock:
    return cast(AsyncMock, unknown)

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from decoy import Decoy, matchers
from wcpan.drive.client._drive import (
    _VIRTUAL_ROOT_ID,
    _SourceState,
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
    Hasher,
    Node,
    NodeQuery,
    NodeSummary,
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
    def setUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)

    async def testCreate(self) -> None:
        backend = create_backend_mock(self.decoy)
        cache = create_cache_mock(self.decoy)

        # each factory only enters when called with the expected service
        create_backend = self.decoy.mock(name="create_backend")
        create_backend_middleware_1 = self.decoy.mock(name="backend_middleware_1")
        create_backend_middleware_2 = self.decoy.mock(name="backend_middleware_2")
        self.decoy.when(create_backend()).then_enter_with(backend)
        self.decoy.when(create_backend_middleware_1(backend)).then_enter_with(backend)
        self.decoy.when(create_backend_middleware_2(backend)).then_enter_with(backend)

        create_cache = self.decoy.mock(name="create_cache")
        create_cache_middleware_1 = self.decoy.mock(name="cache_middleware_1")
        create_cache_middleware_2 = self.decoy.mock(name="cache_middleware_2")
        self.decoy.when(create_cache()).then_enter_with(cache)
        self.decoy.when(create_cache_middleware_1(cache)).then_enter_with(cache)
        self.decoy.when(create_cache_middleware_2(cache)).then_enter_with(cache)

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
            pass

    async def testMultiMode(self) -> None:
        async with create_multi_drive(
            sources=[
                SourceConfig(
                    name="google",
                    backend=as_factory(create_backend_mock(self.decoy)),
                    cache=as_factory(create_cache_mock(self.decoy)),
                ),
                SourceConfig(
                    name="local",
                    backend=as_factory(create_backend_mock(self.decoy)),
                    cache=as_factory(create_cache_mock(self.decoy)),
                ),
            ]
        ) as drive:
            root = await drive.get_root()
            self.assertEqual(root.id, _VIRTUAL_ROOT_ID)
            self.assertIsNone(root.parent_id)
            self.assertTrue(root.is_directory)

    async def testMultiModeRejectsDuplicateNames(self) -> None:
        create_backend = self.decoy.mock(name="create_backend")
        create_cache = self.decoy.mock(name="create_cache")
        source = SourceConfig(name="google", backend=create_backend, cache=create_cache)

        with self.assertRaises(ValueError):
            async with create_multi_drive(sources=[source, source]):
                pass
        self.decoy.verify(create_backend(), times=0)
        self.decoy.verify(create_cache(), times=0)


class _DriveTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)
        self._fs = create_backend_mock(self.decoy)
        self._ss = create_cache_mock(self.decoy)
        self.decoy.when(await self._fs.is_authenticated()).then_return(True)
        self._drive = await self.enterAsyncContext(
            create_drive(backend=as_factory(self._fs), cache=as_factory(self._ss))
        )


class AuthTestCase(_DriveTestCase):
    async def testIsAuthenticated(self) -> None:
        rv = await self._drive.is_authenticated()

        self.assertTrue(rv)

    async def testAuthenticate(self) -> None:
        await self._drive.authenticate()

        self.decoy.verify(await self._fs.authenticate())


class GetHasherTestCase(_DriveTestCase):
    async def testGetHasherFactory(self) -> None:
        node = make_node(node_id="abc")
        self.decoy.when(await self._fs.get_hasher_factory(node)).then_return(
            _create_hasher_1
        )
        rv = await self._drive.get_hasher_factory(node)

        self.assertIs(rv, _create_hasher_1)


class CacheTestCase(_DriveTestCase):
    async def testGetRoot(self) -> None:
        root = make_node(node_id="root", is_directory=True)
        self.decoy.when(await self._ss.get_root()).then_return(root)
        rv = await self._drive.get_root()

        self.assertEqual(rv, root)

    async def testGetNodeById(self) -> None:
        node = make_node(node_id="42")
        self.decoy.when(await self._ss.get_node_by_id("42")).then_return(node)
        rv = await self._drive.get_node_by_id("42")

        self.assertEqual(rv, node)

    async def testGetNodeByPath(self) -> None:
        node = make_node(node_id="42")
        path = PurePath("/a/b/c")
        self.decoy.when(await self._ss.get_node_by_path(path)).then_return(node)
        rv = await self._drive.get_node_by_path(path)

        self.assertEqual(rv, node)

    async def testGetChildByName(self) -> None:
        node = make_node(node_id="42")
        parent = make_node(node_id="456", is_directory=True)
        self.decoy.when(await self._ss.get_child_by_name("123", "456")).then_return(
            node
        )
        rv = await self._drive.get_child_by_name("123", parent)

        self.assertEqual(rv, node)

    async def testGetChildrenById(self) -> None:
        children = [make_node(node_id="42")]
        parent = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._ss.get_children_by_id("123")).then_return(children)
        rv = await self._drive.get_children(parent)

        self.assertEqual(rv, children)

    async def testGetTrashedNodes(self) -> None:
        self.decoy.when(await self._ss.get_trashed_nodes()).then_return([])
        rv = await self._drive.get_trashed_nodes()

        self.assertEqual(rv, [])

    async def testGetTrashedNodesFlatten(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        self.decoy.when(await self._ss.get_trashed_nodes()).then_return(
            [trashed_dir, child]
        )

        rv = await self._drive.get_trashed_nodes(flatten=True)

        self.assertEqual(rv, [trashed_dir, child])

    async def testGetTrashedNodesFiltered(self) -> None:
        trashed_dir = make_node(
            node_id="d1", parent_id=None, name="dir", is_directory=True, is_trashed=True
        )
        child = make_node(
            node_id="f1", parent_id="d1", name="file.txt", is_trashed=True
        )
        self.decoy.when(await self._ss.get_trashed_nodes()).then_return(
            [trashed_dir, child]
        )

        rv = await self._drive.get_trashed_nodes(flatten=False)

        self.assertEqual(rv, [trashed_dir])

    async def testResolvePath(self) -> None:
        path = PurePath("/a")
        node = make_node(node_id="123")
        self.decoy.when(await self._ss.resolve_path_by_id("123")).then_return(path)
        rv = await self._drive.resolve_path(node)

        self.assertEqual(rv, path)

    async def testFindNodes(self) -> None:
        self.decoy.when(
            await self._ss.query_nodes(NodeQuery(name_pattern="123"))
        ).then_return([])
        rv = await self._drive.find_nodes().name_matches("123").all()

        self.assertEqual(rv, [])

    async def testSummarizeNodes(self):
        summary = NodeSummary(count=2, total_size=42)
        self.decoy.when(
            await self._ss.summarize_nodes(NodeQuery(is_directory=False))
        ).then_return(summary)
        result = await self._drive.find_nodes().files_only().summarize()
        self.assertEqual(result, summary)

    async def testSummaryRejectsLimit(self):
        with self.assertRaises(ValueError):
            await self._drive.summarize_nodes(NodeQuery(limit=1))
        self.decoy.verify(await self._ss.summarize_nodes(matchers.Anything()), times=0)


class WalkTestCase(_DriveTestCase):
    async def testNotFolder(self) -> None:
        node = make_node(node_id="123")

        async for _r, _d, _f in self._drive.walk(node):
            pass

        self.decoy.verify(
            await self._ss.get_children_by_id(matchers.Anything()), times=0
        )

    async def testSuccess(self) -> None:
        node = make_node(node_id="123", is_directory=True)
        directory = make_node(node_id="456", is_directory=True)
        file = make_node(node_id="789")
        self.decoy.when(await self._ss.get_children_by_id("123")).then_return(
            [directory, file]
        )
        self.decoy.when(await self._ss.get_children_by_id("456")).then_return([])

        rv: list[object] = []
        async for r, d, f in self._drive.walk(node):
            rv.append((r, d, f))

        self.assertEqual(rv, [(node, [directory], [file]), (directory, [], [])])


class MoveTestCase(_DriveTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.decoy.when(await self._ss.get_root()).then_return(
            make_node(node_id="root", is_directory=True)
        )

    async def _verify_not_moved(self) -> None:
        self.decoy.verify(
            await self._fs.move(
                matchers.Anything(),
                new_parent=matchers.AnythingOrNone(),
                new_name=matchers.AnythingOrNone(),
            ),
            times=0,
        )

    async def testMoveRootNode(self) -> None:
        node = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._ss.get_root()).then_return(node)
        new_parent = make_node(node_id="456", is_directory=True)

        with self.assertRaises(ValueError):
            await self._drive.move(node, new_parent=new_parent, new_name="123")
        await self._verify_not_moved()

    async def testUnauthorized(self) -> None:
        node = make_node(node_id="123")
        new_parent = make_node(node_id="456", is_directory=True)
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            await self._drive.move(node, new_parent=new_parent, new_name="123")

    async def testNoArgs(self) -> None:
        node = make_node(node_id="123")

        with self.assertRaises(ValueError):
            await self._drive.move(node)
        await self._verify_not_moved()

    async def testMoveToNewParent(self) -> None:
        node = make_node(node_id="123")
        new_parent = make_node(node_id="456", is_directory=True)
        moved = make_node(node_id="123", parent_id="456")
        self.decoy.when(
            await self._fs.move(node, new_parent=new_parent, new_name=None)
        ).then_return(moved)

        with patch("wcpan.drive.client._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent)

        self.assertEqual(rv, moved)

    async def testMoveToNewName(self) -> None:
        node = make_node(node_id="123")
        moved = make_node(node_id="123", name="456")
        self.decoy.when(
            await self._fs.move(node, new_parent=None, new_name="456")
        ).then_return(moved)

        rv = await self._drive.move(node, new_name="456")

        self.assertEqual(rv, moved)

    async def testMoveToNewParentAndNewName(self) -> None:
        node = make_node(node_id="123")
        new_parent = make_node(node_id="456", is_directory=True)
        moved = make_node(node_id="123", parent_id="456", name="789")
        self.decoy.when(
            await self._fs.move(node, new_parent=new_parent, new_name="789")
        ).then_return(moved)

        with patch("wcpan.drive.client._drive._contains") as contains:
            contains.return_value = False
            rv = await self._drive.move(node, new_parent=new_parent, new_name="789")

        self.assertEqual(rv, moved)


class PurgeTrashTestCase(_DriveTestCase):
    async def testUnauthorized(self) -> None:
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            await self._drive.purge_trash()

    async def testSuccess(self) -> None:
        await self._drive.purge_trash()
        self.decoy.verify(await self._fs.purge_trash())


class DeleteTestCase(_DriveTestCase):
    async def testUnauthorized(self) -> None:
        node = make_node(node_id="123")
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            await self._drive.delete(node)

    async def testSuccess(self) -> None:
        node = make_node(node_id="123")

        await self._drive.delete(node)
        self.decoy.verify(await self._fs.delete(node, permanent=False))

    async def testPermanent(self) -> None:
        node = make_node(node_id="123")

        await self._drive.delete(node, permanent=True)
        self.decoy.verify(await self._fs.delete(node, permanent=True))


class RestoreTestCase(_DriveTestCase):
    async def testUnauthorized(self) -> None:
        node = make_node(node_id="123")
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            await self._drive.restore(node)

    async def testSuccess(self) -> None:
        node = make_node(node_id="123", is_trashed=True)
        restored = make_node(node_id="123")
        self.decoy.when(await self._fs.restore(node)).then_return(restored)

        rv = await self._drive.restore(node)

        self.assertEqual(rv, restored)


class CreateDirectoryTestCase(_DriveTestCase):
    async def testInvalidParent(self) -> None:
        parent = make_node(node_id="123")

        with self.assertRaises(ValueError):
            await self._drive.create_directory("123", parent)

    async def testInvalidName(self) -> None:
        parent = make_node(node_id="123", is_directory=True)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a/b", parent)

        with self.assertRaises(ValueError):
            await self._drive.create_directory("a\\b", parent)

    async def testUnauthorized(self) -> None:
        parent = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            await self._drive.create_directory("123", parent)

    async def testConflicted(self) -> None:
        parent = make_node(node_id="123", is_directory=True)
        node = make_node(node_id="456", name="aaa")
        self.decoy.when(await self._ss.get_child_by_name("123", "123")).then_return(
            node
        )

        with self.assertRaises(NodeExistsError):
            await self._drive.create_directory("123", parent)

    async def testSuccess(self) -> None:
        parent = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._ss.get_child_by_name("123", "123")).then_raise(
            NodeNotFoundError("")
        )
        node = make_node(node_id="456", is_directory=True)
        self.decoy.when(
            await self._fs.create_directory("123", parent, exist_ok=False, private=None)
        ).then_return(node)

        rv = await self._drive.create_directory("123", parent)

        self.assertEqual(rv, node)


class DownloadFileTestCase(_DriveTestCase):
    async def testNotFile(self) -> None:
        node = make_node(node_id="123", is_directory=True)

        with self.assertRaises(ValueError):
            async with self._drive.download_file(node):
                pass

    async def testUnauthorized(self) -> None:
        node = make_node(node_id="123")
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            async with self._drive.download_file(node):
                pass

    async def testSuccess(self) -> None:
        node = make_node(node_id="123")
        fin = self.decoy.mock(cls=ReadableFile)
        self.decoy.when(self._fs.download_file(node)).then_enter_with(fin)

        async with self._drive.download_file(node) as rv:
            self.assertEqual(rv, fin)


class UploadFileTestCase(_DriveTestCase):
    async def testUnauthorized(self) -> None:
        parent = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testNotFolder(self) -> None:
        parent = make_node(node_id="123")

        with self.assertRaises(ValueError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testInvalidName(self) -> None:
        parent = make_node(node_id="123", is_directory=True)

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
        parent = make_node(node_id="123", is_directory=True)
        node = make_node(node_id="789", name="456")
        self.decoy.when(await self._ss.get_child_by_name("123", "123")).then_return(
            node
        )

        with self.assertRaises(NodeExistsError):
            async with self._drive.upload_file("123", parent):
                pass

    async def testSuccess(self) -> None:
        parent = make_node(node_id="123", is_directory=True)
        self.decoy.when(await self._ss.get_child_by_name("123", "123")).then_raise(
            NodeNotFoundError("123")
        )
        fout = self.decoy.mock(cls=WritableFile)
        self.decoy.when(
            self._fs.upload_file(
                "123",
                parent,
                size=123,
                mime_type="text/plain",
                media_info=None,
                private=None,
            )
        ).then_enter_with(fout)

        async with self._drive.upload_file(
            "123", parent, size=123, mime_type="text/plain"
        ) as rv:
            self.assertEqual(rv, fout)


class SyncTestCase(_DriveTestCase):
    async def testUnauthorized(self) -> None:
        self.decoy.when(await self._fs.is_authenticated()).then_return(False)

        with self.assertRaises(AuthenticationError):
            async for _ in self._drive.sync():
                pass

    async def testResetRoot(self) -> None:
        self.decoy.when(await self._fs.get_initial_cursor()).then_return("123")
        self.decoy.when(await self._ss.get_current_cursor()).then_return("")
        node = make_node(node_id="root", is_directory=True)
        self.decoy.when(await self._fs.get_root()).then_return(node)
        self.decoy.when(self._fs.get_changes("123")).then_return(to_async_iterable([]))
        async for _ in self._drive.sync():
            pass

        self.decoy.verify(await self._ss.set_root(node), times=1)

    async def testApply(self) -> None:
        self.decoy.when(await self._fs.get_initial_cursor()).then_return("123")
        self.decoy.when(await self._ss.get_current_cursor()).then_return("456")
        actions: list[ChangeAction] = [(True, "123")]
        self.decoy.when(self._fs.get_changes("456")).then_return(
            to_async_iterable([(actions, "789")])
        )
        rv: list[ChangeAction] = []
        async for _ in self._drive.sync():
            rv.append(_)

        self.assertEqual(rv, [(True, "123")])
        self.decoy.verify(await self._ss.apply_changes([(True, "123")], "789"), times=1)


# --- Multi-source (_MultiDrive) tests ---


class _MultiDriveTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)
        self._drive, self._sources = await self.enterAsyncContext(
            create_mocked_multi_drive(["google", "local"], decoy=self.decoy)
        )


class MultiDriveGetRootTestCase(_MultiDriveTestCase):
    async def testGetRoot(self) -> None:
        root = await self._drive.get_root()
        self.assertEqual(root.id, _VIRTUAL_ROOT_ID)
        self.assertIsNone(root.parent_id)
        self.assertTrue(root.is_directory)
        self.assertFalse(root.is_trashed)


class MultiDriveGetChildrenTestCase(_MultiDriveTestCase):
    async def testGetChildrenOfRoot(self) -> None:
        google_root = make_node(node_id="groot", name="", is_directory=True)
        local_root = make_node(node_id="lroot", name="", is_directory=True)
        self.decoy.when(await self._sources["google"].cache.get_root()).then_return(
            google_root
        )
        self.decoy.when(await self._sources["local"].cache.get_root()).then_return(
            local_root
        )

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
        self.decoy.when(
            await self._sources["google"].cache.get_children_by_id("groot")
        ).then_return([backend_child])

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


class MultiDriveGetChildByNameTestCase(_MultiDriveTestCase):
    async def testGetChildByNameFromVirtualRoot(self) -> None:
        backend_root = make_node(node_id="groot", name="", is_directory=True)
        self.decoy.when(await self._sources["google"].cache.get_root()).then_return(
            backend_root
        )

        virtual_root = await self._drive.get_root()
        node = await self._drive.get_child_by_name("google", virtual_root)

        self.assertEqual(node.id, "google:groot")
        self.assertEqual(node.parent_id, _VIRTUAL_ROOT_ID)
        self.assertEqual(node.name, "google")


class MultiDriveGetNodeByPathTestCase(_MultiDriveTestCase):
    async def testGetRootPath(self) -> None:
        root = await self._drive.get_node_by_path(PurePath("/"))
        self.assertEqual(root.id, _VIRTUAL_ROOT_ID)

    async def testGetSourceRootPath(self) -> None:
        backend_root = make_node(node_id="groot", is_directory=True)
        self.decoy.when(await self._sources["google"].cache.get_root()).then_return(
            backend_root
        )

        node = await self._drive.get_node_by_path(PurePath("/google"))
        self.assertEqual(node.id, "google:groot")
        self.assertEqual(node.parent_id, _VIRTUAL_ROOT_ID)
        self.assertEqual(node.name, "google")

    async def testGetDeepPath(self) -> None:
        backend_node = make_node(
            node_id="file1", parent_id="docs", name="file.txt", is_directory=False
        )
        self.decoy.when(
            await self._sources["google"].cache.get_node_by_path(
                PurePath("/docs/file.txt")
            )
        ).then_return(backend_node)

        node = await self._drive.get_node_by_path(PurePath("/google/docs/file.txt"))
        self.assertEqual(node.id, "google:file1")

    async def testUnknownSource(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            await self._drive.get_node_by_path(PurePath("/unknown/path"))


class MultiDriveResolvePathTestCase(_MultiDriveTestCase):
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
        self.decoy.when(
            await self._sources["google"].cache.resolve_path_by_id("file1")
        ).then_return(PurePath("/docs/file.txt"))

        node = make_node(
            node_id="google:file1",
            parent_id="google:docs",
            name="file.txt",
            is_directory=False,
        )
        path = await self._drive.resolve_path(node)
        self.assertEqual(path, PurePath("/google/docs/file.txt"))


class MultiDriveSyncTestCase(_MultiDriveTestCase):
    async def testSyncYieldsScopedChanges(self) -> None:
        google_fs = self._sources["google"].backend
        google_ss = self._sources["google"].cache
        local_fs = self._sources["local"].backend
        local_ss = self._sources["local"].cache

        backend_node = make_node(node_id="file1", parent_id="root1", name="file.txt")
        self.decoy.when(await google_fs.get_initial_cursor()).then_return("cur0")
        self.decoy.when(await google_ss.get_current_cursor()).then_return("cur1")
        google_actions: list[ChangeAction] = [(False, backend_node), (True, "old_id")]
        self.decoy.when(google_fs.get_changes("cur1")).then_return(
            to_async_iterable([(google_actions, "cur2")])
        )

        self.decoy.when(await local_fs.get_initial_cursor()).then_return("lcur0")
        self.decoy.when(await local_ss.get_current_cursor()).then_return("lcur1")
        self.decoy.when(local_fs.get_changes("lcur1")).then_return(
            to_async_iterable([])
        )

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
        self.decoy.when(
            await self._sources["google"].backend.is_authenticated()
        ).then_return(False)

        with self.assertRaises(AuthenticationError):
            async for _ in self._drive.sync():
                pass

    async def testSyncBothSourcesEmitChanges(self) -> None:
        google_fs = self._sources["google"].backend
        google_ss = self._sources["google"].cache
        local_fs = self._sources["local"].backend
        local_ss = self._sources["local"].cache

        google_node = make_node(node_id="g1", parent_id="groot", name="google_file.txt")
        local_node = make_node(node_id="l1", parent_id="lroot", name="local_file.txt")

        self.decoy.when(await google_fs.get_initial_cursor()).then_return("gcur0")
        self.decoy.when(await google_ss.get_current_cursor()).then_return("gcur1")
        google_actions: list[ChangeAction] = [(False, google_node), (True, "gremoved")]
        self.decoy.when(google_fs.get_changes("gcur1")).then_return(
            to_async_iterable([(google_actions, "gcur2")])
        )

        self.decoy.when(await local_fs.get_initial_cursor()).then_return("lcur0")
        self.decoy.when(await local_ss.get_current_cursor()).then_return("lcur1")
        local_actions: list[ChangeAction] = [(False, local_node), (True, "lremoved")]
        self.decoy.when(local_fs.get_changes("lcur1")).then_return(
            to_async_iterable([(local_actions, "lcur2")])
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


class MultiDriveMoveTestCase(_MultiDriveTestCase):
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

        with self.assertRaises(ValueError, msg="cannot move across sources"):
            await self._drive.move(google_node, new_parent=local_parent)

    async def testMoveVirtualRootRaises(self) -> None:
        virtual_root = await self._drive.get_root()

        with self.assertRaises(ValueError):
            await self._drive.move(virtual_root, new_name="newname")

    async def testMoveSourceRootRaises(self) -> None:
        source_root = make_node(
            node_id="google:root",
            parent_id=_VIRTUAL_ROOT_ID,
            name="google",
            is_directory=True,
        )

        with self.assertRaises(ValueError):
            await self._drive.move(source_root, new_name="newname")


class MultiDriveFindNodesTestCase(_MultiDriveTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._google = self._sources["google"].cache
        self._local = self._sources["local"].cache

    async def testWithoutAncestorQueriesAllSources(self) -> None:
        query = NodeQuery(name_pattern="photo")
        self.decoy.when(await self._google.query_nodes(query)).then_return(
            [make_node(node_id="g1", parent_id="groot", name="photo.jpg")]
        )
        self.decoy.when(await self._local.query_nodes(query)).then_return(
            [make_node(node_id="l1", parent_id="lroot", name="photo.png")]
        )

        results = await self._drive.find_nodes().name_matches(r"photo").all()

        self.assertEqual({n.id for n in results}, {"google:g1", "local:l1"})
        self.assertEqual(
            {n.parent_id for n in results}, {"google:groot", "local:lroot"}
        )

    async def testVirtualRootAncestorQueriesAllSources(self) -> None:
        self.decoy.when(await self._google.query_nodes(NodeQuery())).then_return(
            [make_node(node_id="g1", parent_id="groot")]
        )
        self.decoy.when(await self._local.query_nodes(NodeQuery())).then_return(
            [make_node(node_id="l1", parent_id="lroot")]
        )
        root = await self._drive.get_root()

        results = await self._drive.find_nodes().descendant_of(root).all()

        self.assertEqual({n.id for n in results}, {"google:g1", "local:l1"})

    async def testScopedAncestorQueriesOneSource(self) -> None:
        self.decoy.when(
            await self._local.query_nodes(NodeQuery(ancestor_id="l1"))
        ).then_return([make_node(node_id="l2", parent_id="l1", name="a.txt")])
        ancestor = make_node(node_id="local:l1", parent_id="local:lroot")

        results = await self._drive.find_nodes().descendant_of(ancestor).all()

        self.assertEqual([n.id for n in results], ["local:l2"])
        self.decoy.verify(await self._google.query_nodes(matchers.Anything()), times=0)

    async def testUnknownSourceRaises(self) -> None:
        ancestor = make_node(node_id="missing:x", parent_id="missing:root")

        with self.assertRaises(NodeNotFoundError):
            await self._drive.find_nodes().descendant_of(ancestor).all()

    async def testLimitTruncatesMergedResults(self) -> None:
        self.decoy.when(await self._google.query_nodes(NodeQuery(limit=1))).then_return(
            [
                make_node(node_id="g1", parent_id="groot"),
                make_node(node_id="g2", parent_id="groot"),
            ]
        )

        results = await self._drive.find_nodes().limit(1).all()

        self.assertEqual(len(results), 1)
        self.decoy.verify(await self._local.query_nodes(matchers.Anything()), times=0)


class MultiDriveSummaryTestCase(_MultiDriveTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._google = self._sources["google"].cache
        self._local = self._sources["local"].cache

    async def _stub_summaries(self, query: NodeQuery) -> None:
        self.decoy.when(await self._google.summarize_nodes(query)).then_return(
            NodeSummary(count=2, total_size=42)
        )
        self.decoy.when(await self._local.summarize_nodes(query)).then_return(
            NodeSummary(count=3, total_size=100)
        )

    async def testCombinesAllSources(self) -> None:
        await self._stub_summaries(NodeQuery(is_directory=False))
        result = await self._drive.find_nodes().files_only().summarize()
        self.assertEqual(result, NodeSummary(count=5, total_size=142))

    async def testVirtualRoot(self) -> None:
        await self._stub_summaries(NodeQuery())
        root = await self._drive.get_root()
        result = await self._drive.find_nodes().descendant_of(root).summarize()
        self.assertEqual(result, NodeSummary(count=5, total_size=142))

    async def testScopedAncestor(self) -> None:
        self.decoy.when(
            await self._local.summarize_nodes(NodeQuery(ancestor_id="l1"))
        ).then_return(NodeSummary(count=3, total_size=100))
        ancestor = make_node(node_id="local:l1")
        result = await self._drive.find_nodes().descendant_of(ancestor).summarize()
        self.assertEqual(result, NodeSummary(count=3, total_size=100))
        self.decoy.verify(
            await self._google.summarize_nodes(matchers.Anything()), times=0
        )

    async def testUnknownSource(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            await self._drive.summarize_nodes(NodeQuery(ancestor_id="missing:x"))

    async def testRejectsLimit(self) -> None:
        with self.assertRaises(ValueError):
            await self._drive.summarize_nodes(NodeQuery(limit=1))
        self.decoy.verify(
            await self._google.summarize_nodes(matchers.Anything()), times=0
        )
        self.decoy.verify(
            await self._local.summarize_nodes(matchers.Anything()), times=0
        )


class MultiDriveGetNodeByIdTestCase(_MultiDriveTestCase):
    async def testGetNodeById(self) -> None:
        backend_node = make_node(node_id="abc", parent_id="groot", name="file.txt")
        self.decoy.when(
            await self._sources["google"].cache.get_node_by_id("abc")
        ).then_return(backend_node)

        result = await self._drive.get_node_by_id("google:abc")

        self.assertEqual(result.id, "google:abc")
        self.assertEqual(result.parent_id, "google:groot")

    async def testGetNodeByIdUnknownSource(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            await self._drive.get_node_by_id("unknown:abc")


class MultiDriveGetTrashedNodesTestCase(_MultiDriveTestCase):
    async def _stub_trashed(self, google: list[Node], local: list[Node]) -> None:
        self.decoy.when(
            await self._sources["google"].cache.get_trashed_nodes()
        ).then_return(google)
        self.decoy.when(
            await self._sources["local"].cache.get_trashed_nodes()
        ).then_return(local)

    async def testGetTrashedNodesEmpty(self) -> None:
        await self._stub_trashed([], [])

        result = await self._drive.get_trashed_nodes()

        self.assertEqual(result, [])

    async def testGetTrashedNodesMerged(self) -> None:
        google_node = make_node(
            node_id="g1", parent_id="groot", name="google_trash.txt", is_trashed=True
        )
        local_node = make_node(
            node_id="l1", parent_id="lroot", name="local_trash.txt", is_trashed=True
        )
        await self._stub_trashed([google_node], [local_node])

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
        await self._stub_trashed([trashed_dir, child], [])

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
        await self._stub_trashed([trashed_dir, child], [])

        result = await self._drive.get_trashed_nodes(flatten=False)

        ids = {n.id for n in result}
        self.assertIn("google:d1", ids)
        self.assertNotIn("google:f1", ids)


class MultiDriveGetHasherTestCase(_MultiDriveTestCase):
    async def testGetHasherFactory(self) -> None:
        node = make_node(node_id="local:file1", parent_id="local:root", name="file.txt")
        # the backend should receive the unscoped node
        self.decoy.when(
            await self._sources["local"].backend.get_hasher_factory(
                matchers.HasAttributes({"id": "file1", "parent_id": "root"})
            )
        ).then_return(_create_hasher_1)
        rv = await self._drive.get_hasher_factory(node)

        self.assertIs(rv, _create_hasher_1)
        self.decoy.verify(
            await self._sources["google"].backend.get_hasher_factory(
                matchers.Anything()
            ),
            times=0,
        )

    async def testGetHasherFactoryRoutesToCorrectSource(self) -> None:
        google_node = make_node(node_id="google:g1", parent_id="google:root")
        local_node = make_node(node_id="local:l1", parent_id="local:root")
        self.decoy.when(
            await self._sources["google"].backend.get_hasher_factory(
                matchers.Anything()
            )
        ).then_return(_create_hasher_1)
        self.decoy.when(
            await self._sources["local"].backend.get_hasher_factory(matchers.Anything())
        ).then_return(_create_hasher_2)

        rv_google = await self._drive.get_hasher_factory(google_node)
        rv_local = await self._drive.get_hasher_factory(local_node)

        self.assertIs(rv_google, _create_hasher_1)
        self.assertIs(rv_local, _create_hasher_2)


class MultiDriveDeleteTestCase(_MultiDriveTestCase):
    async def testUnauthorized(self) -> None:
        self.decoy.when(
            await self._sources["google"].backend.is_authenticated()
        ).then_return(False)
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

        # the backend should receive the unscoped node
        self.decoy.verify(
            await self._sources["google"].backend.delete(
                matchers.HasAttributes({"id": "file1"}), permanent=False
            ),
            times=1,
        )


async def _create_hasher_1() -> Hasher:
    raise NotImplementedError


async def _create_hasher_2() -> Hasher:
    raise NotImplementedError


def as_factory[T](service: T) -> Callable[[], AbstractAsyncContextManager[T]]:
    return lambda: nullcontext(service)


def create_backend_mock(decoy: Decoy) -> Backend:
    backend = decoy.mock(cls=Backend)
    decoy.when(backend.api_version).then_return(7)
    return backend


def create_cache_mock(decoy: Decoy) -> Cache:
    cache = decoy.mock(cls=Cache)
    decoy.when(cache.api_version).then_return(7)
    return cache


async def to_async_iterable[T](rv: Iterable[T]) -> AsyncIterator[T]:
    for _ in rv:
        yield _


@asynccontextmanager
async def create_mocked_multi_drive(
    source_names: list[str], *, decoy: Decoy
) -> AsyncGenerator[tuple[Any, dict[str, _SourceState]]]:
    source_configs: list[SourceConfig] = []
    source_states: dict[str, _SourceState] = {}

    for name in source_names:
        fs = create_backend_mock(decoy)
        # Default: authenticated
        decoy.when(await fs.is_authenticated()).then_return(True)
        ss = create_cache_mock(decoy)

        source_configs.append(
            SourceConfig(
                name=name,
                backend=as_factory(fs),
                cache=as_factory(ss),
            )
        )
        source_states[name] = _SourceState(backend=fs, cache=ss)

    async with create_multi_drive(sources=source_configs) as drive:
        yield drive, source_states

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePath
from tempfile import NamedTemporaryFile
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.core.exceptions import NodeNotFoundError
from wcpan.drive.core.types import ChangeAction, Node
from wcpan.drive.sqlite._lib import read_only, read_write
from wcpan.drive.sqlite._outer import (
    KEY_CURSOR,
    inner_get_node_by_id,
    inner_insert_node,
    inner_set_metadata,
)
from wcpan.drive.sqlite._service import create_service


class GetCurrentCursorTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testWithoutInitialize(self):
        rv = await self._ss.get_current_cursor()
        self.assertEqual(rv, "")

    async def testAfterInitialize(self):
        with read_write(self._dsn) as query:
            inner_set_metadata(query, KEY_CURSOR, "42")

        rv = await self._ss.get_current_cursor()
        self.assertEqual(rv, "42")


class RootTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testGetWithoutInitialize(self):
        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_root()

    async def testSetRoot(self):
        node = _make_root("123")
        await self._ss.set_root(node)

        with read_only(self._dsn) as query:
            rv = inner_get_node_by_id(query, "123")

        self.assertIsNotNone(rv)

    async def testGetRoot(self):
        node = _make_root("123")
        await self._ss.set_root(node)
        rv = await self._ss.get_root()

        self.assertIsNotNone(rv)
        assert rv
        self.assertEqual(rv, node)


class GetNodeTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testIdNotFound(self):
        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_node_by_id("123")

    async def testPathNotFound(self):
        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_node_by_path(PurePath("/123"))

    async def testInvalidPath(self):
        with self.assertRaises(ValueError):
            await self._ss.get_node_by_path(PurePath("123"))

    async def testRootPath(self):
        node = _make_root("root")
        await self._ss.set_root(node)

        rv = await self._ss.get_node_by_path(PurePath("/"))
        self.assertEqual(rv, node)

    async def testGetByPath(self):
        node = _make_root("1")
        await self._ss.set_root(node)
        with read_write(self._dsn) as query:
            node = _make_dir("2", "1", "a")
            inner_insert_node(query, node)
            node = _make_file("3", "2", "b")
            inner_insert_node(query, node)

        rv = await self._ss.get_node_by_path(PurePath("/a/b"))
        self.assertEqual(rv, node)

    async def testGetById(self):
        node = _make_root("root")
        await self._ss.set_root(node)

        rv = await self._ss.get_node_by_id("root")
        self.assertEqual(rv, node)


class ResolvePathTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testGoodId(self):
        node = _make_root("1")
        await self._ss.set_root(node)
        with read_write(self._dsn) as query:
            node = _make_dir("2", "1", "a")
            inner_insert_node(query, node)
            node = _make_file("3", "2", "b")
            inner_insert_node(query, node)

        rv = await self._ss.resolve_path_by_id("3")
        self.assertEqual(rv, PurePath("/a/b"))

    async def testRootId(self):
        node = _make_root("1")
        await self._ss.set_root(node)

        rv = await self._ss.resolve_path_by_id("1")
        self.assertEqual(rv, PurePath("/"))

    async def testBadId(self):
        node = _make_root("1")
        await self._ss.set_root(node)
        with read_write(self._dsn) as query:
            node = _make_dir("2", "1", "a")
            inner_insert_node(query, node)
            node = _make_file("3", "2", "b")
            inner_insert_node(query, node)

        with self.assertRaises(NodeNotFoundError):
            await self._ss.resolve_path_by_id("4")


class GetChildrenTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testGetChild(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            inner_insert_node(query, a)
            b = _make_file("3", "2", "b")
            inner_insert_node(query, b)

        rv = await self._ss.get_child_by_name("b", "2")
        self.assertEqual(rv, b)

    async def testGetChildFromWrongParent(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            inner_insert_node(query, a)
            b = _make_file("3", "2", "b")
            inner_insert_node(query, b)

        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_child_by_name("b", "1")

    async def testGetChildWithNoNode(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            inner_insert_node(query, a)
            b = _make_file("3", "2", "b")
            inner_insert_node(query, b)

        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_child_by_name("b", "4")

    async def testGetChildren(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            inner_insert_node(query, a)
            b = _make_file("3", "2", "b")
            inner_insert_node(query, b)
            c = _make_file("4", "2", "c")
            inner_insert_node(query, c)

        rv = await self._ss.get_children_by_id("2")
        rv = sorted(rv, key=lambda x: x.name)
        self.assertEqual(rv, [b, c])

    async def testGetNoChildren(self):
        root = _make_root("1")
        await self._ss.set_root(root)

        rv = await self._ss.get_children_by_id("1")
        self.assertEqual(rv, [])

    async def testGetChildrenWithWrongId(self):
        root = _make_root("1")
        await self._ss.set_root(root)

        # TODO maybe should raise exception
        rv = await self._ss.get_children_by_id("2")
        self.assertEqual(rv, [])


class SearchNodesTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testGetTrashedNode(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            a = replace(a, is_trashed=True)
            inner_insert_node(query, a)
            b = _make_file("3", "1", "b")
            b = replace(b, is_trashed=True)
            inner_insert_node(query, b)

        rv = await self._ss.get_trashed_nodes()
        rv = sorted(rv, key=lambda x: x.name)
        self.assertEqual(rv, [a, b])

    async def testSearchByRegex(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        with read_write(self._dsn) as query:
            a = _make_dir("2", "1", "a")
            inner_insert_node(query, a)
            b = _make_file("3", "1", "b")
            inner_insert_node(query, b)
            c = _make_file("4", "1", "c")
            inner_insert_node(query, c)

        rv = await self._ss.find_nodes_by_regex(r"a|b")
        rv = sorted(rv, key=lambda x: x.name)
        self.assertEqual(rv, [a, b])


class ApplyChangesTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_service(dsn=self._dsn))

    async def testPatches(self):
        root = _make_root("1")
        await self._ss.set_root(root)
        change_list: list[ChangeAction] = [
            (False, _make_dir("2", "1", "a")),
            (False, _make_file("3", "2", "b")),
            (False, _make_file("4", "2", "c")),
            (True, "3"),
        ]
        await self._ss.apply_changes(change_list, "point")

        with self.assertRaises(NodeNotFoundError):
            await self._ss.get_node_by_path(PurePath("/a/b"))

        rv = await self._ss.get_node_by_path(PurePath("/a/c"))
        self.assertEqual(rv.id, "4")


def _make_root(id: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=None,
        name="",
        is_directory=True,
        is_trashed=False,
        ctime=now,
        mtime=now,
        mime_type="",
        hash="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )


def _make_dir(id: str, parent_id: str, name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=parent_id,
        name=name,
        is_directory=True,
        is_trashed=False,
        ctime=now,
        mtime=now,
        mime_type="",
        hash="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )


def _make_file(id: str, parent_id: str, name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=parent_id,
        name=name,
        is_directory=False,
        is_trashed=False,
        ctime=now,
        mtime=now,
        mime_type="application/octet-stream",
        hash="__hash__",
        size=42,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePath
from tempfile import NamedTemporaryFile
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from wcpan.drive.client.exceptions import NodeNotFoundError
from wcpan.drive.client.sqlite._lib import read_only, read_write
from wcpan.drive.client.sqlite._outer import (
    KEY_CURSOR,
    inner_get_node_by_id,
    inner_insert_node,
    inner_set_metadata,
)
from wcpan.drive.client.sqlite._service import create_cache
from wcpan.drive.client.sqlite.exceptions import SqliteCacheError
from wcpan.drive.client.types import ChangeAction, Node, NodeQuery, NodeSummary


class GetCurrentCursorTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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
            b = _make_file("3", "1", "B")
            inner_insert_node(query, b)
            c = _make_file("4", "1", "c")
            inner_insert_node(query, c)

        rv = await self._ss.query_nodes(NodeQuery(name_pattern=r"a|b"))
        rv = sorted(rv, key=lambda x: x.name)
        self.assertEqual(rv, [b, a])


class QueryNodesTestCase(IsolatedAsyncioTestCase):
    """Tree used by every test:

    1 (root)
    +-- 2 a/
    |   +-- 3 b      42 bytes, application/octet-stream
    |   +-- 4 c/
    |       +-- 5 d  0 bytes, video/mp4, modified 2025-06-01
    +-- 6 e          trashed
    +-- 7 f/         trashed
        +-- 8 g      trashed (inherited from f)
    """

    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

        await self._ss.set_root(_make_root("1"))
        with read_write(self._dsn) as query:
            for node in (
                _make_dir("2", "1", "a"),
                _make_file("3", "2", "b"),
                _make_dir("4", "2", "c"),
                replace(
                    _make_file("5", "4", "d"),
                    size=0,
                    mime_type="video/mp4",
                    modified_time=datetime(2025, 6, 1, tzinfo=UTC),
                ),
                replace(_make_file("6", "1", "e"), is_trashed=True),
                replace(_make_dir("7", "1", "f"), is_trashed=True),
                replace(_make_file("8", "7", "g"), is_trashed=True),
            ):
                inner_insert_node(query, node)

    async def _ids(self, **kwargs: Any) -> set[str]:
        nodes = await self._ss.query_nodes(NodeQuery(**kwargs))
        return {_.id for _ in nodes}

    async def testExcludesTrashedByDefault(self):
        self.assertEqual(await self._ids(), {"1", "2", "3", "4", "5"})

    async def testIncludeTrashed(self):
        self.assertEqual(
            await self._ids(is_trashed=None), {"1", "2", "3", "4", "5", "6", "7", "8"}
        )

    async def testTrashedOnly(self):
        self.assertEqual(await self._ids(is_trashed=True), {"6", "7", "8"})

    async def testDescendantsExcludeAncestor(self):
        self.assertEqual(await self._ids(ancestor_id="2"), {"3", "4", "5"})

    async def testDescendantsOfFile(self):
        self.assertEqual(await self._ids(ancestor_id="3"), set())

    async def testUnknownAncestorRaises(self):
        with self.assertRaises(NodeNotFoundError):
            await self._ss.query_nodes(NodeQuery(ancestor_id="404"))

    async def testFilesOnly(self):
        self.assertEqual(
            await self._ids(ancestor_id="2", is_directory=False), {"3", "5"}
        )

    async def testDirectoriesOnly(self):
        self.assertEqual(await self._ids(is_directory=True), {"1", "2", "4"})

    async def testSizeOnlyMatchesFiles(self):
        self.assertEqual(await self._ids(size_max=0), {"5"})
        self.assertEqual(await self._ids(size_min=42, size_max=42), {"3"})

    async def testMimeTypePrefix(self):
        self.assertEqual(await self._ids(mime_type_prefix="video/"), {"5"})
        self.assertEqual(await self._ids(mime_type_prefix="VIDEO/"), set())

    async def testMimeTypePrefixIsLiteral(self):
        self.assertEqual(await self._ids(mime_type_prefix="%"), set())
        self.assertEqual(await self._ids(mime_type_prefix="video_"), set())

    async def testModifiedRange(self):
        start = datetime(2025, 6, 1, tzinfo=UTC)
        end = datetime(2025, 6, 2, tzinfo=UTC)
        self.assertEqual(
            await self._ids(modified_after=start, modified_before=end), {"5"}
        )
        self.assertEqual(await self._ids(modified_before=start), set())

    async def testNameAndDescendant(self):
        self.assertEqual(await self._ids(ancestor_id="4", name_pattern="^D$"), {"5"})

    async def testLimit(self):
        self.assertEqual(len(await self._ids(limit=2)), 2)

    async def testParentCycleTerminates(self):
        with read_write(self._dsn) as query:
            inner_insert_node(query, _make_dir("9", "10", "x"))
            inner_insert_node(query, _make_dir("10", "9", "y"))

        self.assertEqual(await self._ids(ancestor_id="9"), {"10"})

    async def testSummaries(self):
        cases = [
            (NodeQuery(), NodeSummary(count=5, total_size=42)),
            (NodeQuery(is_trashed=None), NodeSummary(count=8, total_size=126)),
            (NodeQuery(is_trashed=True), NodeSummary(count=3, total_size=84)),
            (NodeQuery(is_directory=True), NodeSummary(count=3, total_size=0)),
            (NodeQuery(is_directory=False), NodeSummary(count=2, total_size=42)),
            (NodeQuery(ancestor_id="2"), NodeSummary(count=3, total_size=42)),
            (NodeQuery(ancestor_id="3"), NodeSummary(count=0, total_size=0)),
            (NodeQuery(name_pattern="^B$"), NodeSummary(count=1, total_size=42)),
            (NodeQuery(name_pattern="missing"), NodeSummary(count=0, total_size=0)),
            (NodeQuery(mime_type_prefix="video/"), NodeSummary(count=1, total_size=0)),
            (NodeQuery(size_min=42), NodeSummary(count=1, total_size=42)),
            (
                NodeQuery(
                    modified_after=datetime(2025, 6, 1, tzinfo=UTC),
                    modified_before=datetime(2025, 6, 2, tzinfo=UTC),
                ),
                NodeSummary(count=1, total_size=0),
            ),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(await self._ss.summarize_nodes(query), expected)

    async def testSummaryCreatedWindowIncludesStartExcludesEndAndIncludesOrphans(self):
        begin = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        with read_write(self._dsn) as query:
            inner_insert_node(
                query,
                replace(
                    _make_file("9", "1", "start"), created_time=begin, is_trashed=True
                ),
            )
            inner_insert_node(
                query, replace(_make_file("10", "1", "end"), created_time=end)
            )
            inner_insert_node(
                query,
                replace(_make_file("11", "missing", "orphan"), created_time=begin),
            )
        result = await self._ss.summarize_nodes(
            NodeQuery(
                is_directory=False,
                is_trashed=None,
                created_after=begin,
                created_before=end,
            )
        )
        self.assertEqual(result, NodeSummary(count=2, total_size=84))

    async def testSummaryDoesNotCountMultipleParentsTwice(self):
        with read_write(self._dsn) as query:
            query.execute(
                "INSERT INTO parents (id, parent_id) VALUES (?, ?)", ("3", "4")
            )
        self.assertEqual(
            await self._ss.summarize_nodes(
                NodeQuery(ancestor_id="2", is_directory=False)
            ),
            NodeSummary(count=2, total_size=42),
        )

    async def testSummaryDoesNotConstructNodes(self):
        from wcpan.drive.client.sqlite._outer import summarize_nodes

        with patch(
            "wcpan.drive.client.sqlite._outer.node_from_query",
            side_effect=AssertionError,
        ):
            self.assertEqual(
                summarize_nodes(self._dsn, NodeQuery()),
                NodeSummary(count=5, total_size=42),
            )

    async def testSummaryUnknownAncestor(self):
        with self.assertRaises(NodeNotFoundError):
            await self._ss.summarize_nodes(NodeQuery(ancestor_id="missing"))

    async def testSummaryRejectsLimit(self):
        with self.assertRaises(ValueError):
            await self._ss.summarize_nodes(NodeQuery(limit=1))


class ApplyChangesTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name
        self._ss = await self.enterAsyncContext(create_cache(dsn=self._dsn))

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


class HashAlgorithmTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(NamedTemporaryFile())
        self._dsn = tmp.name

    async def testAlgorithmPersists(self):
        async with create_cache(dsn=self._dsn) as ss:
            await ss.set_root(_make_root("1"))
            await ss.apply_changes(
                [
                    (False, _make_file("2", "1", "a")),
                    (False, replace(_make_file("3", "1", "b"), hash_algorithm="md4")),
                ],
                "1",
            )
        async with create_cache(dsn=self._dsn) as ss:
            a = await ss.get_node_by_id("2")
            b = await ss.get_node_by_id("3")

        self.assertEqual(a.hash_algorithm, "md5")
        self.assertEqual(b.hash_algorithm, "md4")

    async def testFileWithoutChecksumStaysFile(self):
        pending = replace(_make_file("2", "1", "a"), hash="", hash_algorithm="")
        async with create_cache(dsn=self._dsn) as ss:
            await ss.set_root(_make_root("1"))
            await ss.apply_changes([(False, pending)], "1")
            rv = await ss.get_node_by_id("2")

        self.assertFalse(rv.is_directory)
        self.assertEqual(rv.hash, "")
        self.assertEqual(rv.hash_algorithm, "")

    async def testVersion6RequiresRebuild(self):
        self._create_version_6()

        with self.assertRaises(SqliteCacheError):
            async with create_cache(dsn=self._dsn):
                pass

    def _create_version_6(self) -> None:
        with read_write(self._dsn) as query:
            query.execute(
                "CREATE TABLE nodes (id TEXT NOT NULL, name TEXT, trashed BOOLEAN, "
                "created_time INTEGER, modified_time INTEGER, changed_time INTEGER, "
                "PRIMARY KEY (id));"
            )
            query.execute(
                "CREATE TABLE files (id TEXT NOT NULL, mime_type TEXT, hash TEXT, "
                "size INTEGER, PRIMARY KEY (id));"
            )
            query.executemany(
                "INSERT INTO nodes VALUES (?, ?, 0, 0, 0, 0);",
                [("2", "a"), ("3", "b"), ("4", "c")],
            )
            query.executemany(
                "INSERT INTO files VALUES (?, 'text/plain', ?, 1);",
                [("2", "abc"), ("3", "")],
            )
            query.execute("PRAGMA user_version = 6;")


def _make_root(id: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=None,
        name="",
        is_directory=True,
        is_trashed=False,
        created_time=now,
        modified_time=now,
        changed_time=now,
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


def _make_dir(id: str, parent_id: str, name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=parent_id,
        name=name,
        is_directory=True,
        is_trashed=False,
        created_time=now,
        modified_time=now,
        changed_time=now,
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


def _make_file(id: str, parent_id: str, name: str) -> Node:
    now = datetime.now(UTC)
    return Node(
        id=id,
        parent_id=parent_id,
        name=name,
        is_directory=False,
        is_trashed=False,
        created_time=now,
        modified_time=now,
        changed_time=now,
        mime_type="application/octet-stream",
        hash="__hash__",
        hash_algorithm="md5",
        size=42,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )

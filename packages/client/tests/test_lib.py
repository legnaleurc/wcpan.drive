from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path, PurePath
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

from decoy import Decoy, matchers
from wcpan.drive.client.exceptions import NodeExistsError, NodeNotFoundError
from wcpan.drive.client.lib import (
    dispatch_change,
    else_none,
    find_duplicate_nodes,
    get_file_hash,
    is_remove,
    is_update,
    is_valid_name,
    move_node,
    normalize_path,
)
from wcpan.drive.client.types import (
    Drive,
    Hasher,
    Node,
    RemoveAction,
    UpdateAction,
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


class TestUtilities(TestCase):
    def testIsValidName(self) -> None:
        ok = is_valid_name("name")
        self.assertTrue(ok)

        ok = is_valid_name("name/name")
        self.assertFalse(ok)

        ok = is_valid_name("./name")
        self.assertFalse(ok)

        ok = is_valid_name("name\\/name")
        self.assertFalse(ok)

        ok = is_valid_name("name\\name")
        self.assertFalse(ok)


class TestNormalizePath(TestCase):
    def testDot(self) -> None:
        result = normalize_path(PurePath("/a/./b"))
        self.assertEqual(result, PurePath("/a/b"))

    def testDotDot(self) -> None:
        result = normalize_path(PurePath("/a/b/../c"))
        self.assertEqual(result, PurePath("/a/c"))

    def testMixed(self) -> None:
        result = normalize_path(PurePath("/a/b/../../c/./d"))
        self.assertEqual(result, PurePath("/c/d"))

    def testRelativeRaises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_path(PurePath("relative"))

    def testRoot(self) -> None:
        result = normalize_path(PurePath("/"))
        self.assertEqual(result, PurePath("/"))


class TestElseNone(IsolatedAsyncioTestCase):
    async def testSuccess(self) -> None:
        mock = AsyncMock(return_value=42)
        result = await else_none(mock())
        self.assertEqual(result, 42)

    async def testNodeNotFound(self) -> None:
        mock = AsyncMock(side_effect=NodeNotFoundError("missing"))
        result = await else_none(mock())
        self.assertIsNone(result)


class TestTypeGuards(TestCase):
    def testIsRemove(self) -> None:
        change: RemoveAction = (True, "some-id")
        self.assertTrue(is_remove(change))
        self.assertFalse(is_update(change))

    def testIsUpdate(self) -> None:
        node = make_node(node_id="abc")
        change: UpdateAction = (False, node)
        self.assertTrue(is_update(change))
        self.assertFalse(is_remove(change))


class TestDispatchChange(TestCase):
    def testDispatchRemove(self) -> None:
        on_remove = MagicMock(return_value="removed")
        on_update = MagicMock(return_value="updated")
        change: RemoveAction = (True, "node-id")

        result = dispatch_change(change, on_remove=on_remove, on_update=on_update)

        self.assertEqual(result, "removed")
        on_remove.assert_called_once_with("node-id")
        on_update.assert_not_called()

    def testDispatchUpdate(self) -> None:
        node = make_node(node_id="abc")
        on_remove = MagicMock(return_value="removed")
        on_update = MagicMock(return_value="updated")
        change: UpdateAction = (False, node)

        result = dispatch_change(change, on_remove=on_remove, on_update=on_update)

        self.assertEqual(result, "updated")
        on_update.assert_called_once_with(node)
        on_remove.assert_not_called()


class TestMoveNode(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)

    async def _make_drive(self, mapping: dict[PurePath, Node | Exception]) -> Drive:
        drive = self.decoy.mock(cls=Drive)
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_do(
            _path_map(mapping)
        )
        return drive

    async def testRenameOnly(self) -> None:
        src_node = make_node(node_id="src", parent_id="par", name="old.txt")
        renamed = make_node(node_id="src", parent_id="par", name="new.txt")
        parent_node = make_node(node_id="par", name="a", is_directory=True)
        drive = await self._make_drive(
            {
                PurePath("/a/old.txt"): src_node,
                PurePath("/a/new.txt"): NodeNotFoundError("/a/new.txt"),
                PurePath("/a"): parent_node,
            }
        )
        self.decoy.when(
            await drive.move(src_node, new_parent=parent_node, new_name="new.txt")
        ).then_return(renamed)

        result = await move_node(drive, PurePath("/a/old.txt"), PurePath("new.txt"))

        self.assertEqual(result, renamed)

    async def testNoop(self) -> None:
        src_node = make_node(node_id="src", parent_id="par", name="file.txt")
        parent_node = make_node(node_id="par", name="a", is_directory=True)
        drive = await self._make_drive(
            {
                PurePath("/a/file.txt"): src_node,
                PurePath("/a"): parent_node,
            }
        )
        self.decoy.when(
            await drive.move(src_node, new_parent=parent_node, new_name=None)
        ).then_return(src_node)

        result = await move_node(drive, PurePath("/a/file.txt"), PurePath("."))

        self.assertEqual(result, src_node)

    async def testMoveToParent(self) -> None:
        src_node = make_node(node_id="src", parent_id="b", name="file.txt")
        grandparent = make_node(node_id="a", name="a", is_directory=True)
        moved = make_node(node_id="src", parent_id="a", name="file.txt")
        drive = await self._make_drive(
            {
                PurePath("/a/b/file.txt"): src_node,
                PurePath("/a"): grandparent,
            }
        )
        self.decoy.when(
            await drive.move(src_node, new_parent=grandparent, new_name=None)
        ).then_return(moved)

        result = await move_node(drive, PurePath("/a/b/file.txt"), PurePath(".."))

        self.assertEqual(result, moved)

    async def testMoveAbsoluteIntoDirectory(self) -> None:
        src_node = make_node(node_id="src", parent_id="a", name="file.txt")
        dst_dir = make_node(node_id="dst", name="dir", is_directory=True)
        moved = make_node(node_id="src", parent_id="dst", name="file.txt")
        drive = await self._make_drive(
            {
                PurePath("/a/file.txt"): src_node,
                PurePath("/b/dir"): dst_dir,
            }
        )
        self.decoy.when(
            await drive.move(src_node, new_parent=dst_dir, new_name=None)
        ).then_return(moved)

        result = await move_node(drive, PurePath("/a/file.txt"), PurePath("/b/dir"))

        self.assertEqual(result, moved)

    async def testMoveAbsoluteNewName(self) -> None:
        src_node = make_node(node_id="src", parent_id="a", name="file.txt")
        new_parent = make_node(node_id="b", name="b", is_directory=True)
        moved = make_node(node_id="src", parent_id="b", name="renamed.txt")
        drive = await self._make_drive(
            {
                PurePath("/a/file.txt"): src_node,
                PurePath("/b/renamed.txt"): NodeNotFoundError("/b/renamed.txt"),
                PurePath("/b"): new_parent,
            }
        )
        self.decoy.when(
            await drive.move(src_node, new_parent=new_parent, new_name="renamed.txt")
        ).then_return(moved)

        result = await move_node(
            drive, PurePath("/a/file.txt"), PurePath("/b/renamed.txt")
        )

        self.assertEqual(result, moved)

    async def testMoveAbsoluteParentMissing(self) -> None:
        src_node = make_node(node_id="src", parent_id="a", name="file.txt")
        drive = await self._make_drive(
            {
                PurePath("/a/file.txt"): src_node,
                PurePath("/missing/new.txt"): NodeNotFoundError("/missing/new.txt"),
                PurePath("/missing"): NodeNotFoundError("/missing"),
            }
        )

        with self.assertRaises(ValueError):
            await move_node(
                drive, PurePath("/a/file.txt"), PurePath("/missing/new.txt")
            )

    async def testMoveAbsoluteDestIsFile(self) -> None:
        src_node = make_node(node_id="src", parent_id="a", name="file.txt")
        dst_file = make_node(
            node_id="dst", parent_id="b", name="existing.txt", is_directory=False
        )
        drive = await self._make_drive(
            {
                PurePath("/a/file.txt"): src_node,
                PurePath("/b/existing.txt"): dst_file,
            }
        )

        with self.assertRaises(NodeExistsError):
            await move_node(drive, PurePath("/a/file.txt"), PurePath("/b/existing.txt"))


class TestFindDuplicateNodes(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)

    async def testNoDuplicates(self) -> None:
        drive = self.decoy.mock(cls=Drive)
        root = make_node(node_id="root", is_directory=True)
        file1 = make_node(node_id="f1", parent_id="root", name="a.txt")
        file2 = make_node(node_id="f2", parent_id="root", name="b.txt")
        self.decoy.when(drive.walk(root)).then_return(
            _async_walk_results([(root, [], [file1, file2])])
        )

        result = await find_duplicate_nodes(drive, root)

        self.assertEqual(result, [])

    async def testWithDuplicates(self) -> None:
        drive = self.decoy.mock(cls=Drive)
        root = make_node(node_id="root", is_directory=True)
        file1 = make_node(node_id="f1", parent_id="root", name="dup.txt")
        file2 = make_node(node_id="f2", parent_id="root", name="dup.txt")
        self.decoy.when(drive.walk(root)).then_return(
            _async_walk_results([(root, [], [file1, file2])])
        )

        result = await find_duplicate_nodes(drive, root)

        self.assertEqual(len(result), 1)
        self.assertIn(file1, result[0])
        self.assertIn(file2, result[0])

    async def testDefaultRoot(self) -> None:
        drive = self.decoy.mock(cls=Drive)
        root = make_node(node_id="root", is_directory=True)
        self.decoy.when(await drive.get_root()).then_return(root)
        self.decoy.when(drive.walk(root)).then_return(
            _async_walk_results([(root, [], [])])
        )

        result = await find_duplicate_nodes(drive)

        self.assertEqual(result, [])


async def _async_walk_results(
    items: list[tuple[Node, list[Node], list[Node]]],
) -> AsyncIterator[tuple[Node, list[Node], list[Node]]]:
    for item in items:
        yield item


def _path_map(
    mapping: dict[PurePath, Node | Exception],
) -> Callable[[PurePath], Node]:
    def action(path: PurePath) -> Node:
        value = mapping.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise NodeNotFoundError(str(path))
        return value

    return action


class NodeHashInvariantTestCase(TestCase):
    def testFileWithBothFields(self) -> None:
        node = replace(make_node(node_id="f"), hash="abc", hash_algorithm="md5")
        self.assertEqual(node.hash_algorithm, "md5")

    def testFileWithoutChecksum(self) -> None:
        node = make_node(node_id="f")
        self.assertEqual(node.hash, "")

    def testFileWithHashOnly(self) -> None:
        with self.assertRaises(ValueError):
            replace(make_node(node_id="f"), hash="abc")

    def testFileWithAlgorithmOnly(self) -> None:
        with self.assertRaises(ValueError):
            replace(make_node(node_id="f"), hash_algorithm="md5")

    def testDirectoryWithHash(self) -> None:
        with self.assertRaises(ValueError):
            replace(
                make_node(node_id="d", is_directory=True),
                hash="abc",
                hash_algorithm="md5",
            )


class _Md5Hasher(Hasher):
    def __init__(self) -> None:
        self._hasher = md5()

    async def update(self, data: bytes) -> None:
        self._hasher.update(data)

    async def digest(self) -> bytes:
        return self._hasher.digest()

    async def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    async def copy(self) -> "_Md5Hasher":
        rv = _Md5Hasher()
        rv._hasher = self._hasher.copy()
        return rv


async def _create_md5_hasher() -> Hasher:
    return _Md5Hasher()


class TestGetFileHash(IsolatedAsyncioTestCase):
    async def testUsesNodeHasher(self) -> None:
        data = b"x" * (300 * 1024)
        node = make_node(node_id="f1", name="a.bin")
        decoy = Decoy()
        self.addCleanup(decoy.reset)
        drive = decoy.mock(cls=Drive)
        decoy.when(await drive.get_hasher_factory(node)).then_return(_create_md5_hasher)

        with TemporaryDirectory() as tmp, ThreadPoolExecutor() as pool:
            path = Path(tmp) / "a.bin"
            path.write_bytes(data)
            rv = await get_file_hash(path, pool=pool, drive=drive, node=node)

        self.assertEqual(rv, md5(data).hexdigest())

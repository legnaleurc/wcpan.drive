from io import StringIO
from pathlib import PurePath
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.client.cli._main import amain
from wcpan.drive.client.types import Drive, NodeQuery, NodeSummary

from ._lib import aexpect, bind_find_nodes, create_amock, make_node


class TestMain(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stderr", new_callable=StringIO)
    async def testMkdirNoArgs(self, stderr: StringIO):
        with self.assertRaises(SystemExit):
            await amain(["mkdir"])

    @patch("sys.stderr", new_callable=StringIO)
    async def testMkdirWithPath(self, stderr: StringIO):
        parent_node = MagicMock()
        aexpect(self._drive.get_node_by_path).return_value = parent_node

        rv = await amain(["-cc.yaml", "mkdir", "/var/log"])
        self.assertEqual(rv, 0)

        path = PurePath("/var/log")
        aexpect(self._drive.get_node_by_path).assert_called_once_with(path.parent)
        aexpect(self._drive.create_directory).assert_called_once_with(
            path.name, parent_node, exist_ok=True
        )


class TestList(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testListByPath(self, stdout: StringIO):
        node = make_node(is_directory=True)
        child = make_node(id="child-id", name="child.txt")
        aexpect(self._drive.get_node_by_path).return_value = node
        aexpect(self._drive.get_children).return_value = [child]

        rv = await amain(["-cc.yaml", "list", "/some/dir"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.get_node_by_path).assert_called_once_with(
            PurePath("/some/dir")
        )
        aexpect(self._drive.get_children).assert_called_once_with(node)
        self.assertIn("child.txt", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testListById(self, stdout: StringIO):
        node = make_node(id="abc123", is_directory=True)
        child = make_node(id="child-id", name="file.txt")
        aexpect(self._drive.get_node_by_id).return_value = node
        aexpect(self._drive.get_children).return_value = [child]

        rv = await amain(["-cc.yaml", "list", "abc123"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.get_node_by_id).assert_called_once_with("abc123")
        self.assertIn("file.txt", stdout.getvalue())

    @patch("sys.stderr", new_callable=StringIO)
    async def testListNoArgs(self, stderr: StringIO):
        with self.assertRaises(SystemExit):
            await amain(["-cc.yaml", "list"])


class TestFind(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testFindIdOnly(self, stdout: StringIO):
        node = make_node(id="abc123", is_trashed=False)
        query_nodes = bind_find_nodes(self._drive)
        query_nodes.return_value = [node]

        rv = await amain(["-cc.yaml", "find", "--id-only", ".*\\.txt"])
        self.assertEqual(rv, 0)

        query_nodes.assert_awaited_once_with(NodeQuery(name_pattern=".*\\.txt"))
        self.assertIn("abc123", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testFindExcludesTrash(self, stdout: StringIO):
        query_nodes = bind_find_nodes(self._drive)
        query_nodes.return_value = []

        rv = await amain(["-cc.yaml", "find", "--id-only", "pattern"])
        self.assertEqual(rv, 0)

        query_nodes.assert_awaited_once_with(
            NodeQuery(name_pattern="pattern", is_trashed=False)
        )

    @patch("sys.stdout", new_callable=StringIO)
    async def testFindIncludeTrash(self, stdout: StringIO):
        active = make_node(id="active-id", is_trashed=False)
        trashed = make_node(id="trashed-id", is_trashed=True)
        query_nodes = bind_find_nodes(self._drive)
        query_nodes.return_value = [active, trashed]

        rv = await amain(
            ["-cc.yaml", "find", "--id-only", "--include-trash", "pattern"]
        )
        self.assertEqual(rv, 0)

        query_nodes.assert_awaited_once_with(
            NodeQuery(name_pattern="pattern", is_trashed=None)
        )
        output = stdout.getvalue()
        self.assertIn("active-id", output)
        self.assertIn("trashed-id", output)

    @patch("sys.stdout", new_callable=StringIO)
    async def testFindWithPath(self, stdout: StringIO):
        node = make_node(id="abc123", is_trashed=False)
        bind_find_nodes(self._drive).return_value = [node]
        aexpect(self._drive.resolve_path).return_value = PurePath("/some/file.txt")

        rv = await amain(["-cc.yaml", "find", "pattern"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("abc123", output)
        self.assertIn("/some/file.txt", output)


class TestInfo(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testInfoByPath(self, stdout: StringIO):
        node = make_node(id="abc123", name="photo.jpg", size=2048)
        aexpect(self._drive.get_node_by_path).return_value = node

        rv = await amain(["-cc.yaml", "info", "/photo.jpg"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("abc123", output)
        self.assertIn("photo.jpg", output)

    @patch("sys.stdout", new_callable=StringIO)
    async def testInfoById(self, stdout: StringIO):
        node = make_node(id="xyz789", name="doc.pdf")
        aexpect(self._drive.get_node_by_id).return_value = node

        rv = await amain(["-cc.yaml", "info", "xyz789"])
        self.assertEqual(rv, 0)

        self.assertIn("xyz789", stdout.getvalue())


class TestTree(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testTreeRootNode(self, stdout: StringIO):
        root = make_node(id="root-id", parent_id=None, name="", is_directory=True)
        aexpect(self._drive.get_node_by_path).return_value = root
        aexpect(self._drive.get_children).return_value = []

        rv = await amain(["-cc.yaml", "tree", "/"])
        self.assertEqual(rv, 0)

        self.assertIn("/", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testTreeWithChildren(self, stdout: StringIO):
        dir_node = make_node(
            id="dir-id", name="mydir", is_directory=True, parent_id="root"
        )
        child = make_node(id="child-id", name="file.txt", is_directory=False)
        aexpect(self._drive.get_node_by_path).return_value = dir_node
        aexpect(self._drive.resolve_path).return_value = PurePath("/mydir")
        aexpect(self._drive.get_children).side_effect = [
            [child],  # children of dir_node
            [],  # children of child (not a dir, won't be called)
        ]

        rv = await amain(["-cc.yaml", "tree", "/mydir"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("mydir", output)
        self.assertIn("file.txt", output)


class TestUsage(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testUsageFile(self, stdout: StringIO):
        file_node = make_node(size=1024, is_directory=False)
        aexpect(self._drive.get_node_by_path).return_value = file_node

        rv = await amain(["-cc.yaml", "usage", "/test.txt"])
        self.assertEqual(rv, 0)

        self.assertIn("1,024", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testUsageFileNoComma(self, stdout: StringIO):
        file_node = make_node(size=1024, is_directory=False)
        aexpect(self._drive.get_node_by_path).return_value = file_node

        rv = await amain(["-cc.yaml", "usage", "--no-comma", "/test.txt"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("1024", output)
        self.assertNotIn("1,024", output)

    @patch("sys.stdout", new_callable=StringIO)
    async def testUsageDirectory(self, stdout: StringIO):
        dir_node = make_node(is_directory=True)
        aexpect(self._drive.get_node_by_path).return_value = dir_node
        bind_find_nodes(self._drive)
        aexpect(self._drive.summarize_nodes).return_value = NodeSummary(
            count=2, total_size=800
        )

        rv = await amain(["-cc.yaml", "usage", "/mydir"])
        self.assertEqual(rv, 0)

        self.assertIn("800", stdout.getvalue())
        aexpect(self._drive.summarize_nodes).assert_awaited_once_with(
            NodeQuery(ancestor_id=dir_node.id, is_directory=False)
        )
        aexpect(self._drive.query_nodes).assert_not_awaited()

    @patch("sys.stderr", new_callable=StringIO)
    @patch("sys.stdout", new_callable=StringIO)
    async def testUsageNotFound(self, stdout: StringIO, stderr: StringIO):
        aexpect(self._drive.get_node_by_path).side_effect = Exception("not found")

        rv = await amain(["-cc.yaml", "usage", "/missing"])
        self.assertEqual(rv, 1)
        self.assertIn("/missing", stderr.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testUsageMultipleSources(self, stdout: StringIO):
        file1 = make_node(id="f1", size=100, is_directory=False)
        file2 = make_node(id="f2", size=200, is_directory=False)
        aexpect(self._drive.get_node_by_path).side_effect = [file1, file2]

        rv = await amain(["-cc.yaml", "usage", "/a.txt", "/b.txt"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("100", output)
        self.assertIn("200", output)


class TestRename(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    async def testRenameByPath(self):
        result_node = make_node(name="new.txt")
        mock_move = self.enterContext(
            patch(
                "wcpan.drive.client.cli._cmd.move.move_node",
                new_callable=AsyncMock,
            )
        )
        mock_move.return_value = result_node

        rv = await amain(["-cc.yaml", "rename", "/old.txt", "new.txt"])
        self.assertEqual(rv, 0)

        mock_move.assert_called_once_with(
            self._drive, PurePath("/old.txt"), PurePath("new.txt")
        )

    @patch("sys.stderr", new_callable=StringIO)
    async def testRenameFailure(self, stderr: StringIO):
        mock_move = self.enterContext(
            patch(
                "wcpan.drive.client.cli._cmd.move.move_node",
                new_callable=AsyncMock,
            )
        )
        mock_move.side_effect = Exception("move failed")
        aexpect(self._drive.get_node_by_path).return_value = make_node()

        rv = await amain(["-cc.yaml", "rename", "/old.txt", "/dest/new.txt"])
        self.assertEqual(rv, 1)
        self.assertIn("move failed", stderr.getvalue())

    async def testRenameById(self):
        src_node = make_node(id="abc123")
        result_node = make_node(name="new.txt")
        aexpect(self._drive.get_node_by_id).return_value = src_node
        aexpect(self._drive.resolve_path).return_value = PurePath("/some/old.txt")
        mock_move = self.enterContext(
            patch(
                "wcpan.drive.client.cli._cmd.move.move_node",
                new_callable=AsyncMock,
            )
        )
        mock_move.return_value = result_node

        rv = await amain(["-cc.yaml", "rename", "abc123", "new.txt"])
        self.assertEqual(rv, 0)

        mock_move.assert_called_once_with(
            self._drive, PurePath("/some/old.txt"), PurePath("new.txt")
        )


class TestRemove(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    async def testTrashNode(self):
        node = make_node()
        aexpect(self._drive.get_node_by_path).return_value = node

        rv = await amain(["-cc.yaml", "remove", "/test.txt"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.delete).assert_called_once_with(node)

    async def testRestoreNode(self):
        node = make_node()
        aexpect(self._drive.get_node_by_path).return_value = node

        rv = await amain(["-cc.yaml", "remove", "--restore", "/test.txt"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.restore).assert_called_once_with(node)

    async def testPurgeNode(self):
        node = make_node()
        aexpect(self._drive.get_node_by_path).return_value = node

        rv = await amain(["-cc.yaml", "remove", "--purge", "/test.txt"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.delete).assert_called_once_with(node, permanent=True)

    @patch("sys.stderr", new_callable=StringIO)
    async def testPurgeAndRestoreConflict(self, stderr: StringIO):
        rv = await amain(["-cc.yaml", "remove", "--purge", "--restore", "/test.txt"])
        self.assertEqual(rv, 1)

    @patch("sys.stderr", new_callable=StringIO)
    async def testRemoveNotFound(self, stderr: StringIO):
        aexpect(self._drive.get_node_by_path).side_effect = Exception("not found")

        rv = await amain(["-cc.yaml", "remove", "/missing.txt"])
        self.assertEqual(rv, 1)
        self.assertIn("/missing.txt", stderr.getvalue())


class TestTrash(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stdout", new_callable=StringIO)
    async def testTrashList(self, stdout: StringIO):
        node = make_node(id="t1", name="deleted.txt", is_trashed=True)
        aexpect(self._drive.get_trashed_nodes).return_value = [node]

        rv = await amain(["-cc.yaml", "trash", "list"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("t1", output)
        self.assertIn("deleted.txt", output)

    @patch("sys.stdout", new_callable=StringIO)
    async def testTrashUsageFiles(self, stdout: StringIO):
        file1 = make_node(id="f1", size=1000, is_directory=False, is_trashed=True)
        file2 = make_node(id="f2", size=500, is_directory=False, is_trashed=True)
        aexpect(self._drive.get_trashed_nodes).return_value = [file1, file2]

        rv = await amain(["-cc.yaml", "trash", "usage"])
        self.assertEqual(rv, 0)

        self.assertIn("1,500", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testTrashUsageNoComma(self, stdout: StringIO):
        file_node = make_node(size=2000, is_directory=False, is_trashed=True)
        aexpect(self._drive.get_trashed_nodes).return_value = [file_node]

        rv = await amain(["-cc.yaml", "trash", "usage", "--no-comma"])
        self.assertEqual(rv, 0)

        output = stdout.getvalue()
        self.assertIn("2000", output)
        self.assertNotIn("2,000", output)

    @patch("sys.stdout", new_callable=StringIO)
    async def testTrashPurgeNoAsk(self, stdout: StringIO):
        aexpect(self._drive.get_trashed_nodes).return_value = [make_node()]

        rv = await amain(["-cc.yaml", "trash", "purge", "-y"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.purge_trash).assert_called_once()
        self.assertIn("Done.", stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    async def testTrashPurgeNoAskFailure(self, stdout: StringIO):
        aexpect(self._drive.get_trashed_nodes).return_value = [make_node()]
        aexpect(self._drive.purge_trash).side_effect = Exception("server error")

        rv = await amain(["-cc.yaml", "trash", "purge", "-y"])
        self.assertEqual(rv, 1)

    @patch("sys.stdout", new_callable=StringIO)
    @patch("builtins.input", return_value="y")
    async def testTrashPurgeWithConfirm(self, mock_input: MagicMock, stdout: StringIO):
        aexpect(self._drive.get_trashed_nodes).return_value = []

        rv = await amain(["-cc.yaml", "trash", "purge"])
        self.assertEqual(rv, 0)

        mock_input.assert_called_once()
        aexpect(self._drive.purge_trash).assert_called_once()

    @patch("sys.stdout", new_callable=StringIO)
    @patch("builtins.input", return_value="n")
    async def testTrashPurgeAborted(self, mock_input: MagicMock, stdout: StringIO):
        aexpect(self._drive.get_trashed_nodes).return_value = []

        rv = await amain(["-cc.yaml", "trash", "purge"])
        self.assertEqual(rv, 0)

        aexpect(self._drive.purge_trash).assert_not_called()
        self.assertIn("Aborted.", stdout.getvalue())


class TestInit(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._factory = self.enterContext(
            patch("wcpan.drive.client.cli._main.create_drive_from_config")
        )

    @patch("sys.stdout", new_callable=StringIO)
    async def testInitNeedsNoConfig(self, stdout: StringIO):
        scaffold = self.enterContext(
            patch(
                "wcpan.drive.client.cli._cmd.init.create_config_scaffold",
                return_value="version: 4\n",
            )
        )

        rv = await amain(["init", "-p", "google", "--plugin", "sqlite"])
        self.assertEqual(rv, 0)

        scaffold.assert_called_once_with(["google", "sqlite"])
        self.assertEqual(stdout.getvalue(), "version: 4\n")
        self._factory.assert_not_called()

    @patch("sys.stderr", new_callable=StringIO)
    async def testInitReportsBadPlugins(self, stderr: StringIO):
        self.enterContext(
            patch(
                "wcpan.drive.client.cli._cmd.init.create_config_scaffold",
                side_effect=ValueError("no plugin matches 'x'"),
            )
        )

        rv = await amain(["init", "-p", "x"])
        self.assertEqual(rv, 1)
        self.assertIn("no plugin matches 'x'", stderr.getvalue())

    @patch("sys.stderr", new_callable=StringIO)
    async def testOtherCommandsNeedConfig(self, stderr: StringIO):
        with self.assertRaises(SystemExit):
            await amain(["list", "/"])
        self.assertIn("--config", stderr.getvalue())
        self._factory.assert_not_called()

"""Filesystem discovery: identity across rescans, removals, and source checks."""

import hashlib
import os
import shutil
from pathlib import Path
from unittest import skipIf
from unittest.mock import patch

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.filesystem._blocking import Blocking
from wcpan.drive.server.filesystem._scan import FilesystemScanner
from wcpan.drive.server.services.mounts import ensure_mount_nodes
from wcpan.drive.server.services.operations import (
    OperationNotSupportedError,
    OperationUnavailableError,
)

from ._harness import DOCS, ScanTestCase


class TestDiscovery(ScanTestCase):
    async def test_publishes_directories_and_files_with_checksums(self) -> None:
        # given
        self.write("a/b.txt", b"hello")
        self.write("pic.jpg", b"jpeg")

        # when
        await self.scan()

        # then
        folder = await self.node("a")
        assert folder is not None
        self.assertTrue(folder.is_directory)
        self.assertEqual(folder.parent_id, DOCS)
        file = await self.node("a/b.txt")
        assert file is not None
        self.assertEqual(file.parent_id, folder.id)
        self.assertEqual(file.size, 5)
        self.assertEqual(file.hash, hashlib.md5(b"hello").hexdigest())
        self.assertEqual(file.hash_algorithm, "md5")
        picture = await self.node("pic.jpg")
        assert picture is not None
        self.assertTrue(picture.is_image)

    async def test_unchanged_rescan_emits_no_changes(self) -> None:
        # given
        self.write("a/b.txt", b"hello")
        await self.scan()
        cursor = await self.cursor()

        # when
        with patch(
            "wcpan.drive.server.filesystem._scan.hash_file",
            side_effect=AssertionError("unchanged files are not rehashed"),
        ):
            await self.scan()

        # then
        self.assertEqual(await self.cursor(), cursor)

    async def test_skips_symlinks(self) -> None:
        # given
        target = self.write("real.txt", b"x")
        (self.source / "link.txt").symlink_to(target)

        # when
        await self.scan()

        # then
        self.assertIsNone(await self.node("link.txt"))
        self.assertIsNotNone(await self.node("real.txt"))


class TestIdentity(ScanTestCase):
    async def test_rename_keeps_node_id(self) -> None:
        # given
        self.write("a/old.txt", b"x")
        await self.scan()
        before = await self.node_id("a/old.txt")

        # when
        os.rename(self.source / "a/old.txt", self.source / "a/new.txt")
        await self.scan()

        # then
        self.assertEqual(await self.node_id("a/new.txt"), before)
        self.assertIsNone(await self.node("a/old.txt"))

    async def test_move_between_folders_keeps_node_id(self) -> None:
        # given
        self.write("a/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        before = await self.node_id("a/f.txt")
        folder_b = await self.node_id("b")

        # when
        os.rename(self.source / "a/f.txt", self.source / "b/f.txt")
        await self.scan()

        # then
        moved = await self.node("b/f.txt")
        assert moved is not None
        self.assertEqual((moved.id, moved.parent_id), (before, folder_b))
        self.assertIsNone(await self.node("a/f.txt"))

    async def test_moved_folder_keeps_subtree_ids(self) -> None:
        # given
        self.write("a/inner/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        inner = await self.node_id("a/inner")
        file = await self.node_id("a/inner/f.txt")

        # when
        os.rename(self.source / "a/inner", self.source / "b/inner")
        await self.scan()

        # then
        self.assertEqual(await self.node_id("b/inner"), inner)
        self.assertEqual(await self.node_id("b/inner/f.txt"), file)

    async def test_replaced_file_gets_new_node(self) -> None:
        # given
        self.write("f.txt", b"old")
        await self.scan()
        before = await self.node_id("f.txt")

        # when
        os.unlink(self.source / "f.txt")
        replacement = self.source / "f.tmp"
        replacement.write_bytes(b"new")
        os.rename(replacement, self.source / "f.txt")
        await self.scan()

        # then
        after = await self.node("f.txt")
        assert after is not None
        self.assertNotEqual(after.id, before)
        self.assertIsNone(await self.mirror.get_node_by_id(before))

    async def test_content_change_updates_same_node(self) -> None:
        # given
        path = self.write("f.txt", b"old")
        await self.scan()
        before = await self.node_id("f.txt")
        cursor = await self.cursor()

        # when
        with open(path, "wb") as f:
            f.write(b"new content")
        await self.scan()

        # then
        after = await self.node("f.txt")
        assert after is not None
        self.assertEqual(after.id, before)
        self.assertEqual(after.hash, hashlib.md5(b"new content").hexdigest())
        changes, _, _ = await self.mirror.get_changes_since(cursor, 100)
        self.assertEqual([c[0] for c in changes], [before])

    async def test_deleted_entries_are_removed(self) -> None:
        # given
        self.write("a/f.txt", b"x")
        await self.scan()
        folder = await self.node_id("a")

        # when
        shutil.rmtree(self.source / "a")
        await self.scan()

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(folder))

    async def test_hard_links_are_separate_nodes(self) -> None:
        # given
        self.write("a/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        first = await self.node_id("a/f.txt")

        # when
        os.link(self.source / "a/f.txt", self.source / "b/g.txt")
        await self.scan()
        # then
        second = await self.node_id("b/g.txt")
        self.assertNotEqual(first, second)
        self.assertEqual(await self.node_id("a/f.txt"), first)

        # when
        os.rename(self.source / "b/g.txt", self.source / "b/h.txt")
        await self.scan()
        # then
        self.assertEqual(await self.node_id("b/h.txt"), second)
        self.assertEqual(await self.node_id("a/f.txt"), first)

    async def test_ids_survive_a_new_scanner(self) -> None:
        # given
        self.write("a/f.txt", b"x")
        await self.scan()
        before = await self.node_id("a/f.txt")
        cursor = await self.cursor()

        # when
        self.scanner = FilesystemScanner(
            mirror=self.mirror,
            store=self.store,
            node_sync=self.node_sync,
            ids=self.ids,
            paths=self.paths,
            blocking=Blocking(executor=self.io_pool),
        )
        await ensure_mount_nodes(
            self.mounts, mirror=self.mirror, node_sync=self.node_sync
        )
        await self.scan()

        # then
        self.assertEqual(await self.node_id("a/f.txt"), before)
        self.assertEqual(await self.cursor(), cursor)


class TestCoverage(ScanTestCase):
    @skipIf(os.geteuid() == 0, "root can list any directory")
    async def test_unlistable_folder_keeps_its_subtree(self) -> None:
        # given
        self.write("a/f.txt", b"x")
        await self.scan()
        file = await self.node_id("a/f.txt")

        # when
        (self.source / "a").chmod(0o000)
        await self.scan()

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(file))

    async def test_missing_source_keeps_mirror(self) -> None:
        # given
        self.write("f.txt", b"x")
        await self.scan()
        file = await self.node_id("f.txt")

        # when
        shutil.rmtree(self.source)
        await self.scan()

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(file))

    async def test_source_on_another_filesystem_keeps_mirror(self) -> None:
        # given
        self.write("f.txt", b"x")
        await self.scan()
        file = await self.node_id("f.txt")

        # when
        # An unmounted source leaves an empty directory on the parent filesystem.
        os.unlink(self.source / "f.txt")
        with patch(
            "wcpan.drive.server.filesystem._scan.filesystem_id",
            return_value="other",
        ):
            await self.scan()

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(file))

    async def test_file_changing_while_hashed_keeps_previous_version(self) -> None:
        # given
        path = self.write("f.txt", b"old")
        await self.scan()
        before = await self.node("f.txt")
        assert before is not None

        # when
        with open(path, "wb") as f:
            f.write(b"newer")
        with patch("wcpan.drive.server.filesystem._scan.hash_file", return_value=None):
            await self.scan()

        # then
        self.assertEqual(await self.node("f.txt"), before)


class TestOperations(ScanTestCase):
    async def read(self, record: NodeRecord, range_: slice | None = None) -> bytes:
        async with self.operations.download(record, range_=range_) as chunks:
            return b"".join([c async for c in chunks])

    async def test_download_and_range(self) -> None:
        # given
        self.write("a/f.txt", b"0123456789")
        await self.scan()
        record = await self.node("a/f.txt")
        assert record is not None

        # when / then
        self.assertEqual(await self.read(record), b"0123456789")
        self.assertEqual(await self.read(record, slice(2, 5)), b"234")
        self.assertEqual(await self.read(record, slice(-3, None)), b"789")

    async def test_download_of_replaced_file_is_unavailable(self) -> None:
        # given
        self.write("f.txt", b"old")
        await self.scan()
        record = await self.node("f.txt")
        assert record is not None

        # when
        os.unlink(self.source / "f.txt")
        self.write("f.txt", b"new")

        # then
        with self.assertRaises(OperationUnavailableError):
            await self.read(record)


class TestReadOnly(ScanTestCase):
    read_only = True

    async def test_changes_are_not_supported(self) -> None:
        # given
        self.write("f.txt", b"x")
        await self.scan()
        record = await self.node("f.txt")
        assert record is not None

        # when / then
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.create_folder(DOCS, "new")
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.update(record, name="other", parent_id=None)
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.delete(record)
        with self.assertRaises(OperationNotSupportedError):
            await self.uploader.find_conflict(DOCS, "g.txt")
        # then
        self.assertTrue((self.source / "f.txt").exists())

"""Filesystem discovery: identity across rescans, removals, and source checks."""

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, skipIf
from unittest.mock import patch

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.filesystem.blocking import Blocking
from wcpan.drive.server.filesystem.operations import FilesystemOperations
from wcpan.drive.server.filesystem.paths import NodePaths
from wcpan.drive.server.filesystem.scan import FilesystemScanner
from wcpan.drive.server.filesystem.store import FILESYSTEM_SCHEMA, FilesystemStore
from wcpan.drive.server.lib.mounts import mount_id
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.operations import (
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.storage import StorageService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)


_DOCS = mount_id("docs")

logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


class _ScanTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wcpan_fs_"))
        self.source = self.tmp / "docs"
        self.source.mkdir()
        self.db_path = str(self.tmp / "mirror.sqlite")
        self.pool = ThreadPoolExecutor(2)
        self.io_pool = ThreadPoolExecutor(2)
        self.storage = StorageService(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            backend=FILESYSTEM_SCHEMA,
        )
        await self.storage.ensure_schema()
        self.write_queue = create_write_queue()
        self.metadata_queue = create_metadata_queue()
        self.node_sync = NodeSyncService(
            storage=self.storage,
            write_queue=self.write_queue,
            off_main=OffMainService(pool=self.pool),
            mounts={"docs": str(self.source)},
            local_paths={},
            metadata_queue=self.metadata_queue,
        )
        self.tasks = [
            asyncio.create_task(write_worker(self.write_queue, noop_after_write)),
            asyncio.create_task(
                metadata_worker(
                    self.metadata_queue, self.node_sync.process_metadata_item
                )
            ),
        ]
        self.store = FilesystemStore(storage=self.storage)
        self.paths = NodePaths(storage=self.storage, mounts={"docs": str(self.source)})
        blocking = Blocking(executor=self.io_pool)
        self.scanner = FilesystemScanner(
            storage=self.storage,
            store=self.store,
            node_sync=self.node_sync,
            paths=self.paths,
            blocking=blocking,
        )
        self.operations = FilesystemOperations(
            paths=self.paths, store=self.store, blocking=blocking
        )
        await self.scanner.ensure_structure()

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.io_pool.shutdown(wait=False, cancel_futures=True)
        for path in self.tmp.rglob("*"):
            with suppress(OSError):
                path.chmod(0o700)
        shutil.rmtree(self.tmp)

    async def scan(self) -> None:
        await self.scanner.scan()
        await self.node_sync.wait_enrichment_drained()

    def write(self, relative: str, data: bytes) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def node(self, relative: str) -> NodeRecord | None:
        """The mirrored node at *relative*, walking names from the mount."""
        parent = _DOCS
        record: NodeRecord | None = None
        for name in relative.split("/"):
            children = await self.storage.get_children(parent)
            record = next((c for c in children if c.name == name), None)
            if record is None:
                return None
            parent = record.id
        return record

    async def node_id(self, relative: str) -> MirrorStableId:
        record = await self.node(relative)
        assert record is not None, relative
        return record.id

    async def cursor(self) -> int:
        return await self.storage.get_cursor()


class TestDiscovery(_ScanTestCase):
    async def test_publishes_directories_and_files_with_checksums(self) -> None:
        self.write("a/b.txt", b"hello")
        self.write("pic.jpg", b"jpeg")

        await self.scan()

        folder = await self.node("a")
        assert folder is not None
        self.assertTrue(folder.is_directory)
        self.assertEqual(folder.parent_id, _DOCS)
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
        self.write("a/b.txt", b"hello")
        await self.scan()
        cursor = await self.cursor()

        with patch(
            "wcpan.drive.server.filesystem.scan.hash_file",
            side_effect=AssertionError("unchanged files are not rehashed"),
        ):
            await self.scan()

        self.assertEqual(await self.cursor(), cursor)

    async def test_skips_symlinks(self) -> None:
        target = self.write("real.txt", b"x")
        (self.source / "link.txt").symlink_to(target)

        await self.scan()

        self.assertIsNone(await self.node("link.txt"))
        self.assertIsNotNone(await self.node("real.txt"))


class TestIdentity(_ScanTestCase):
    async def test_rename_keeps_node_id(self) -> None:
        self.write("a/old.txt", b"x")
        await self.scan()
        before = await self.node_id("a/old.txt")

        os.rename(self.source / "a/old.txt", self.source / "a/new.txt")
        await self.scan()

        self.assertEqual(await self.node_id("a/new.txt"), before)
        self.assertIsNone(await self.node("a/old.txt"))

    async def test_move_between_folders_keeps_node_id(self) -> None:
        self.write("a/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        before = await self.node_id("a/f.txt")
        folder_b = await self.node_id("b")

        os.rename(self.source / "a/f.txt", self.source / "b/f.txt")
        await self.scan()

        moved = await self.node("b/f.txt")
        assert moved is not None
        self.assertEqual((moved.id, moved.parent_id), (before, folder_b))
        self.assertIsNone(await self.node("a/f.txt"))

    async def test_moved_folder_keeps_subtree_ids(self) -> None:
        self.write("a/inner/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        inner = await self.node_id("a/inner")
        file = await self.node_id("a/inner/f.txt")

        os.rename(self.source / "a/inner", self.source / "b/inner")
        await self.scan()

        self.assertEqual(await self.node_id("b/inner"), inner)
        self.assertEqual(await self.node_id("b/inner/f.txt"), file)

    async def test_replaced_file_gets_new_node(self) -> None:
        self.write("f.txt", b"old")
        await self.scan()
        before = await self.node_id("f.txt")

        os.unlink(self.source / "f.txt")
        replacement = self.source / "f.tmp"
        replacement.write_bytes(b"new")
        os.rename(replacement, self.source / "f.txt")
        await self.scan()

        after = await self.node("f.txt")
        assert after is not None
        self.assertNotEqual(after.id, before)
        self.assertIsNone(await self.storage.get_node_by_id(before))

    async def test_content_change_updates_same_node(self) -> None:
        path = self.write("f.txt", b"old")
        await self.scan()
        before = await self.node_id("f.txt")
        cursor = await self.cursor()

        with open(path, "wb") as f:
            f.write(b"new content")
        await self.scan()

        after = await self.node("f.txt")
        assert after is not None
        self.assertEqual(after.id, before)
        self.assertEqual(after.hash, hashlib.md5(b"new content").hexdigest())
        changes, _, _ = await self.storage.get_changes_since(cursor, 100)
        self.assertEqual([c[0] for c in changes], [before])

    async def test_deleted_entries_are_removed(self) -> None:
        self.write("a/f.txt", b"x")
        await self.scan()
        folder = await self.node_id("a")

        shutil.rmtree(self.source / "a")
        await self.scan()

        self.assertIsNone(await self.storage.get_node_by_id(folder))

    async def test_hard_links_are_separate_nodes(self) -> None:
        self.write("a/f.txt", b"x")
        (self.source / "b").mkdir()
        await self.scan()
        first = await self.node_id("a/f.txt")

        os.link(self.source / "a/f.txt", self.source / "b/g.txt")
        await self.scan()
        second = await self.node_id("b/g.txt")
        self.assertNotEqual(first, second)
        self.assertEqual(await self.node_id("a/f.txt"), first)

        os.rename(self.source / "b/g.txt", self.source / "b/h.txt")
        await self.scan()
        self.assertEqual(await self.node_id("b/h.txt"), second)
        self.assertEqual(await self.node_id("a/f.txt"), first)

    async def test_ids_survive_a_new_scanner(self) -> None:
        self.write("a/f.txt", b"x")
        await self.scan()
        before = await self.node_id("a/f.txt")
        cursor = await self.cursor()

        self.scanner = FilesystemScanner(
            storage=self.storage,
            store=self.store,
            node_sync=self.node_sync,
            paths=self.paths,
            blocking=Blocking(executor=self.io_pool),
        )
        await self.scanner.ensure_structure()
        await self.scan()

        self.assertEqual(await self.node_id("a/f.txt"), before)
        self.assertEqual(await self.cursor(), cursor)


class TestCoverage(_ScanTestCase):
    @skipIf(os.geteuid() == 0, "root can list any directory")
    async def test_unlistable_folder_keeps_its_subtree(self) -> None:
        self.write("a/f.txt", b"x")
        await self.scan()
        file = await self.node_id("a/f.txt")

        (self.source / "a").chmod(0o000)
        await self.scan()

        self.assertIsNotNone(await self.storage.get_node_by_id(file))

    async def test_missing_source_keeps_mirror(self) -> None:
        self.write("f.txt", b"x")
        await self.scan()
        file = await self.node_id("f.txt")

        shutil.rmtree(self.source)
        await self.scan()

        self.assertIsNotNone(await self.storage.get_node_by_id(file))

    async def test_source_on_another_filesystem_keeps_mirror(self) -> None:
        self.write("f.txt", b"x")
        await self.scan()
        file = await self.node_id("f.txt")

        # An unmounted source leaves an empty directory on the parent filesystem.
        os.unlink(self.source / "f.txt")
        with patch(
            "wcpan.drive.server.filesystem.scan.filesystem_id",
            return_value="other",
        ):
            await self.scan()

        self.assertIsNotNone(await self.storage.get_node_by_id(file))

    async def test_file_changing_while_hashed_keeps_previous_version(self) -> None:
        path = self.write("f.txt", b"old")
        await self.scan()
        before = await self.node("f.txt")
        assert before is not None

        with open(path, "wb") as f:
            f.write(b"newer")
        with patch("wcpan.drive.server.filesystem.scan.hash_file", return_value=None):
            await self.scan()

        self.assertEqual(await self.node("f.txt"), before)


class TestOperations(_ScanTestCase):
    async def read(self, record: NodeRecord, range_: slice | None = None) -> bytes:
        async with self.operations.download(record, range_=range_) as chunks:
            return b"".join([c async for c in chunks])

    async def test_download_and_range(self) -> None:
        self.write("a/f.txt", b"0123456789")
        await self.scan()
        record = await self.node("a/f.txt")
        assert record is not None

        self.assertEqual(await self.read(record), b"0123456789")
        self.assertEqual(await self.read(record, slice(2, 5)), b"234")
        self.assertEqual(await self.read(record, slice(-3, None)), b"789")

    async def test_download_of_replaced_file_is_unavailable(self) -> None:
        self.write("f.txt", b"old")
        await self.scan()
        record = await self.node("f.txt")
        assert record is not None

        os.unlink(self.source / "f.txt")
        self.write("f.txt", b"new")

        with self.assertRaises(OperationUnavailableError):
            await self.read(record)

    async def test_mutations_are_not_supported(self) -> None:
        self.write("f.txt", b"x")
        await self.scan()
        record = await self.node("f.txt")
        assert record is not None

        with self.assertRaises(OperationNotSupportedError):
            await self.operations.create_folder(_DOCS, "new")
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.update(record, name="other", parent_id=None)
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.delete(record)

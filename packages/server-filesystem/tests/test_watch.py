"""Watched directories are re-read when inotify reports changes."""

import asyncio
import hashlib
import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.backend import CoreServices
from wcpan.drive.server.filesystem import scan as scan_module
from wcpan.drive.server.filesystem.backend import FilesystemRuntime
from wcpan.drive.server.filesystem.inotify import IN_Q_OVERFLOW, Event

from ._harness import ScanTestCase


class _WatchTestCase(ScanTestCase):
    watch = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if self.watcher is None:
            self.skipTest("inotify is not available")

    async def follow(self) -> None:
        """Apply pending change events the way the runtime does, once."""
        assert self.watcher is not None
        async with asyncio.timeout(5):
            await self.watcher.wait()
        await asyncio.sleep(0.05)
        dirty, overflow = self.watcher.take()
        self.assertFalse(overflow)
        await self.scanner.scan_folders(dirty)
        await self.node_sync.wait_enrichment_drained()


class TestFollowChanges(_WatchTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.write("a/inner/deep.txt", b"deep")
        (self.source / "b").mkdir()
        await self.scan()
        assert self.watcher is not None
        self.watcher.take()  # changes made before the initial scan

    async def test_new_file_appears(self) -> None:
        self.write("a/new.txt", b"new")
        await self.follow()
        self.assertIsNotNone(await self.node("a/new.txt"))

    async def test_new_folder_is_read_recursively(self) -> None:
        self.write("a/fresh/one/two.txt", b"2")
        await self.follow()
        self.assertIsNotNone(await self.node("a/fresh/one/two.txt"))

    async def test_new_folder_is_watched(self) -> None:
        (self.source / "a/fresh").mkdir()
        await self.follow()
        self.write("a/fresh/later.txt", b"later")
        await self.follow()
        self.assertIsNotNone(await self.node("a/fresh/later.txt"))

    async def test_scoped_update_keeps_unlisted_subtrees(self) -> None:
        deep = await self.node_id("a/inner/deep.txt")
        self.write("top.txt", b"top")
        await self.follow()
        self.assertIsNotNone(await self.node("top.txt"))
        self.assertIsNotNone(await self.mirror.get_node_by_id(deep))

    async def test_rename_keeps_node_id(self) -> None:
        before = await self.node_id("a/inner/deep.txt")
        os.rename(self.source / "a/inner/deep.txt", self.source / "a/inner/x.txt")
        await self.follow()
        self.assertEqual(await self.node_id("a/inner/x.txt"), before)
        self.assertIsNone(await self.node("a/inner/deep.txt"))

    async def test_move_between_folders_keeps_node_id(self) -> None:
        before = await self.node_id("a/inner/deep.txt")
        os.rename(self.source / "a/inner/deep.txt", self.source / "b/deep.txt")
        await self.follow()
        self.assertEqual(await self.node_id("b/deep.txt"), before)
        self.assertIsNone(await self.node("a/inner/deep.txt"))

    async def test_moved_folder_stays_watched(self) -> None:
        inner = await self.node_id("a/inner")
        os.rename(self.source / "a/inner", self.source / "b/inner")
        await self.follow()
        self.assertEqual(await self.node_id("b/inner"), inner)

        self.write("b/inner/after.txt", b"after")
        await self.follow()
        self.assertIsNotNone(await self.node("b/inner/after.txt"))

    async def test_deletions_are_removed(self) -> None:
        folder = await self.node_id("a/inner")
        shutil.rmtree(self.source / "a/inner")
        await self.follow()
        self.assertIsNone(await self.mirror.get_node_by_id(folder))

    async def test_replaced_folder_gets_new_node(self) -> None:
        old_folder = await self.node_id("a/inner")
        old_file = await self.node_id("a/inner/deep.txt")
        shutil.rmtree(self.source / "a/inner")
        self.write("a/inner/other.txt", b"other")
        await self.follow()

        self.assertIsNone(await self.mirror.get_node_by_id(old_folder))
        self.assertIsNone(await self.mirror.get_node_by_id(old_file))
        self.assertNotEqual(await self.node_id("a/inner"), old_folder)
        self.assertIsNotNone(await self.node("a/inner/other.txt"))
        self.assertIsNone(await self.node("a/inner/deep.txt"))

    async def test_content_change_is_picked_up(self) -> None:
        with open(self.source / "a/inner/deep.txt", "wb") as f:
            f.write(b"rewritten")
        await self.follow()
        record = await self.node("a/inner/deep.txt")
        assert record is not None
        self.assertEqual(record.hash, hashlib.md5(b"rewritten").hexdigest())


class TestRuntime(_WatchTestCase):
    def runtime(self) -> FilesystemRuntime:
        return FilesystemRuntime(
            operations=self.operations,
            uploader=self.uploader,
            scanner=self.scanner,
            watcher=self.watcher,
            rescan_interval=3600,
            core=CoreServices(
                mirror=self.mirror, node_sync=self.node_sync, off_main=MagicMock()
            ),
            settle_delay=0.05,
        )

    async def until(self, relative: str) -> None:
        async with asyncio.timeout(5):
            while await self.node(relative) is None:
                await asyncio.sleep(0.02)

    async def test_changes_after_ready_are_applied(self) -> None:
        runtime = self.runtime()
        await runtime.run_until_ready()
        task = asyncio.create_task(runtime.run())
        try:
            self.write("later/file.txt", b"x")
            await self.until("later/file.txt")
        finally:
            task.cancel()

    async def test_changes_during_startup_scan_are_applied(self) -> None:
        real_list = scan_module.list_directory
        made = False

        def list_then_change(path: str):  # noqa: ANN202
            nonlocal made
            entries = real_list(path)
            if not made and path == str(self.source):
                made = True
                (self.source / "racy.txt").write_bytes(b"x")
            return entries

        runtime = self.runtime()
        with patch.object(scan_module, "list_directory", list_then_change):
            await runtime.run_until_ready()
        self.assertIsNone(await self.node("racy.txt"))
        task = asyncio.create_task(runtime.run())
        try:
            await self.until("racy.txt")
        finally:
            task.cancel()

    async def test_lost_events_trigger_full_rescan(self) -> None:
        assert self.watcher is not None
        runtime = self.runtime()
        await runtime.run_until_ready()
        full_scan = AsyncMock()
        with (
            patch.object(self.scanner, "scan", full_scan),
            patch.object(
                self.watcher._inotify,  # pyright: ignore[reportPrivateUsage]
                "read_events",
                return_value=iter([Event(-1, IN_Q_OVERFLOW, "")]),
            ),
        ):
            task = asyncio.create_task(runtime.run())
            try:
                self.watcher._on_readable()  # pyright: ignore[reportPrivateUsage]
                async with asyncio.timeout(5):
                    while not full_scan.await_count:
                        await asyncio.sleep(0.02)
            finally:
                task.cancel()

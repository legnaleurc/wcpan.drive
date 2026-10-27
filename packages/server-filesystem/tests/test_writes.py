"""Changes made through the server: folders, renames, moves, deletes, uploads."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from unittest.mock import patch

from wcpan.drive.protocol.types import MediaInfo
from wcpan.drive.server.filesystem.reading import STAGING_PREFIX
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID
from wcpan.drive.server.services.operations import (
    OperationConflictError,
    OperationInvalidError,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.upload import UploadConflictError

from ._harness import DOCS, ScanTestCase


async def _content(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _failing_content() -> AsyncIterator[bytes]:
    yield b"partial"
    raise ConnectionResetError("client went away")


class _WriteTestCase(ScanTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.write("a/f.txt", b"hello")
        (self.source / "b").mkdir()
        await self.scan()

    def staging_files(self) -> list[str]:
        return [
            p.name for p in self.source.rglob("*") if p.name.startswith(STAGING_PREFIX)
        ]

    async def assert_rescan_is_quiet(self) -> None:
        """A full pass after the write finds nothing new to record."""
        cursor = await self.cursor()
        await self.scan()
        self.assertEqual(await self.cursor(), cursor)


class TestCreateFolder(_WriteTestCase):
    async def test_creates_directory_and_node(self) -> None:
        record = await self.operations.create_folder(await self.node_id("a"), "new")

        self.assertTrue((self.source / "a/new").is_dir())
        self.assertEqual(await self.node_id("a/new"), record.id)
        await self.assert_rescan_is_quiet()

    async def test_existing_name_conflicts_with_its_node(self) -> None:
        with self.assertRaises(OperationConflictError) as cm:
            await self.operations.create_folder(DOCS, "a")
        assert cm.exception.record is not None
        self.assertEqual(cm.exception.record.id, await self.node_id("a"))

    async def test_invalid_names_are_rejected(self) -> None:
        for name in ("", ".", "..", "x/y", f"{STAGING_PREFIX}x"):
            with self.subTest(name=name), self.assertRaises(OperationInvalidError):
                await self.operations.create_folder(DOCS, name)

    async def test_server_root_is_not_writable(self) -> None:
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.create_folder(SERVER_ROOT_ID, "x")


class TestRenameAndMove(_WriteTestCase):
    async def test_rename_keeps_id_and_checksum_without_rehashing(self) -> None:
        record = await self.node("a/f.txt")
        assert record is not None

        with patch(
            "wcpan.drive.server.filesystem.scan.hash_file",
            side_effect=AssertionError("a rename does not change content"),
        ):
            updated = await self.operations.update(record, name="g.txt", parent_id=None)

        self.assertEqual((updated.id, updated.name), (record.id, "g.txt"))
        self.assertEqual(updated.hash, record.hash)
        self.assertTrue((self.source / "a/g.txt").exists())
        self.assertFalse((self.source / "a/f.txt").exists())
        await self.assert_rescan_is_quiet()

    async def test_rename_onto_existing_name_conflicts(self) -> None:
        self.write("a/taken.txt", b"other")
        await self.scan()
        record = await self.node("a/f.txt")
        assert record is not None

        with self.assertRaises(OperationConflictError):
            await self.operations.update(record, name="taken.txt", parent_id=None)

        self.assertEqual((self.source / "a/taken.txt").read_bytes(), b"other")
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"hello")

    async def test_move_folder_keeps_subtree(self) -> None:
        folder = await self.node("a")
        assert folder is not None
        file_id = await self.node_id("a/f.txt")

        await self.operations.update(
            folder, name=None, parent_id=await self.node_id("b")
        )

        self.assertTrue((self.source / "b/a/f.txt").exists())
        self.assertEqual(await self.node_id("b/a/f.txt"), file_id)
        await self.assert_rescan_is_quiet()

    async def test_move_into_own_subtree_is_invalid(self) -> None:
        (self.source / "a/inner").mkdir()
        await self.scan()
        folder = await self.node("a")
        assert folder is not None

        with self.assertRaises(OperationInvalidError):
            await self.operations.update(
                folder, name=None, parent_id=await self.node_id("a/inner")
            )

    async def test_stale_node_is_not_renamed(self) -> None:
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")
        self.write("a/f.txt", b"someone else's file")

        with self.assertRaises(OperationUnavailableError):
            await self.operations.update(record, name="g.txt", parent_id=None)

        self.assertTrue((self.source / "a/f.txt").exists())


class TestDelete(_WriteTestCase):
    async def test_deletes_file_permanently(self) -> None:
        record = await self.node("a/f.txt")
        assert record is not None

        await self.operations.delete(record)

        self.assertFalse((self.source / "a/f.txt").exists())
        self.assertIsNone(await self.mirror.get_node_by_id(record.id))

    async def test_deletes_folder_with_contents(self) -> None:
        folder = await self.node("a")
        assert folder is not None
        file_id = await self.node_id("a/f.txt")

        await self.operations.delete(folder)

        self.assertFalse((self.source / "a").exists())
        self.assertIsNone(await self.mirror.get_node_by_id(file_id))

    async def test_replaced_entry_is_not_deleted(self) -> None:
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")
        self.write("a/f.txt", b"someone else's file")

        with self.assertRaises(OperationUnavailableError):
            await self.operations.delete(record)

        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"someone else's file")

    async def test_entry_already_gone_is_removed_from_mirror(self) -> None:
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")

        await self.operations.delete(record)

        self.assertIsNone(await self.mirror.get_node_by_id(record.id))


class TestUpload(_WriteTestCase):
    async def test_upload_creates_file(self) -> None:
        record = await self.uploader.upload(
            await self.node_id("b"),
            "new.bin",
            _content(b"part one ", b"part two"),
            mime_type=None,
            media_info=None,
        )

        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"part one part two")
        self.assertEqual(record.hash, hashlib.md5(b"part one part two").hexdigest())
        self.assertEqual(await self.node_id("b/new.bin"), record.id)
        self.assertEqual(self.staging_files(), [])
        await self.assert_rescan_is_quiet()

    async def test_upload_keeps_client_media_info(self) -> None:
        record = await self.uploader.upload(
            DOCS,
            "pic.jpg",
            _content(b"jpeg"),
            mime_type="image/jpeg",
            media_info=MediaInfo(
                width=4, height=3, ms_duration=0, is_image=True, is_video=False
            ),
        )
        self.assertEqual((record.width, record.height), (4, 3))

    async def test_upload_onto_existing_name_conflicts(self) -> None:
        existing = await self.node_id("a/f.txt")

        with self.assertRaises(UploadConflictError) as cm:
            await self.uploader.upload(
                await self.node_id("a"),
                "f.txt",
                _content(b"replacement"),
                mime_type=None,
                media_info=None,
            )

        self.assertEqual(cm.exception.record.id, existing)
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"hello")
        self.assertEqual(self.staging_files(), [])

    async def test_find_conflict_reports_existing_node(self) -> None:
        conflict = await self.uploader.find_conflict(await self.node_id("a"), "f.txt")
        assert conflict is not None
        self.assertEqual(conflict.id, await self.node_id("a/f.txt"))
        self.assertIsNone(await self.uploader.find_conflict(DOCS, "free.txt"))

    async def test_interrupted_upload_leaves_nothing(self) -> None:
        with self.assertRaises(ConnectionResetError):
            await self.uploader.upload(
                DOCS, "broken.bin", _failing_content(), mime_type=None, media_info=None
            )

        self.assertFalse((self.source / "broken.bin").exists())
        self.assertEqual(self.staging_files(), [])
        self.assertIsNone(await self.node("broken.bin"))

    async def test_staging_files_are_not_mirrored(self) -> None:
        self.write(f"b/{STAGING_PREFIX}leftover", b"x")
        await self.scan()
        children = await self.mirror.get_children(await self.node_id("b"))
        self.assertEqual(children, [])


class TestWritesWithWatcher(_WriteTestCase):
    watch = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if self.watcher is None:
            self.skipTest("inotify is not available")

    async def test_watched_write_is_not_mirrored_twice(self) -> None:
        assert self.watcher is not None
        self.watcher.take()
        created = await self.operations.create_folder(DOCS, "fresh")
        uploaded = await self.uploader.upload(
            created.id, "f.txt", _content(b"x"), mime_type=None, media_info=None
        )

        async with asyncio.timeout(5):
            await self.watcher.wait()
        await asyncio.sleep(0.05)
        dirty, _ = self.watcher.take()
        await self.scanner.scan_folders(dirty)
        await self.node_sync.wait_enrichment_drained()

        root_names = [c.name for c in await self.mirror.get_children(DOCS)]
        self.assertEqual(root_names.count("fresh"), 1)
        self.assertEqual(await self.node_id("fresh"), created.id)
        self.assertEqual(await self.node_id("fresh/f.txt"), uploaded.id)

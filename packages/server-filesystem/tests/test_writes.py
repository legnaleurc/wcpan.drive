"""Changes made through the server: folders, renames, moves, deletes, uploads."""

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

from wcpan.drive.protocol.types import MediaInfo, NodeRecord
from wcpan.drive.server.filesystem.operations import FilesystemFile
from wcpan.drive.server.filesystem.reading import STAGING_PREFIX
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID
from wcpan.drive.server.services.operations import (
    OperationConflictError,
    OperationInvalidError,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.upload import (
    UploadConflictError,
    UploadParentGoneError,
    UploadTransientError,
)

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
        # when
        record = await self.operations.create_folder(await self.node_id("a"), "new")

        # then
        self.assertTrue((self.source / "a/new").is_dir())
        self.assertEqual(await self.node_id("a/new"), record.id)
        await self.assert_rescan_is_quiet()

    async def test_existing_name_conflicts_with_its_node(self) -> None:
        # when / then
        with self.assertRaises(OperationConflictError) as cm:
            await self.operations.create_folder(DOCS, "a")
        # then
        assert cm.exception.record is not None
        self.assertEqual(cm.exception.record.id, await self.node_id("a"))

    async def test_invalid_names_are_rejected(self) -> None:
        # given
        for name in ("", ".", "..", "x/y", f"{STAGING_PREFIX}x"):
            # when / then
            with self.subTest(name=name), self.assertRaises(OperationInvalidError):
                await self.operations.create_folder(DOCS, name)

    async def test_server_root_is_not_writable(self) -> None:
        # when / then
        with self.assertRaises(OperationNotSupportedError):
            await self.operations.create_folder(SERVER_ROOT_ID, "x")


class TestRenameAndMove(_WriteTestCase):
    async def test_rename_keeps_id_and_checksum_without_rehashing(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None

        # when
        with patch(
            "wcpan.drive.server.filesystem.scan.hash_file",
            side_effect=AssertionError("a rename does not change content"),
        ):
            updated = await self.operations.update(record, name="g.txt", parent_id=None)

        # then
        self.assertEqual((updated.id, updated.name), (record.id, "g.txt"))
        self.assertEqual(updated.hash, record.hash)
        self.assertTrue((self.source / "a/g.txt").exists())
        self.assertFalse((self.source / "a/f.txt").exists())
        await self.assert_rescan_is_quiet()

    async def test_rename_onto_existing_name_conflicts(self) -> None:
        # given
        self.write("a/taken.txt", b"other")
        await self.scan()
        record = await self.node("a/f.txt")
        assert record is not None

        # when / then
        with self.assertRaises(OperationConflictError):
            await self.operations.update(record, name="taken.txt", parent_id=None)

        # then
        self.assertEqual((self.source / "a/taken.txt").read_bytes(), b"other")
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"hello")

    async def test_move_folder_keeps_subtree(self) -> None:
        # given
        folder = await self.node("a")
        assert folder is not None
        file_id = await self.node_id("a/f.txt")

        # when
        await self.operations.update(
            folder, name=None, parent_id=await self.node_id("b")
        )

        # then
        self.assertTrue((self.source / "b/a/f.txt").exists())
        self.assertEqual(await self.node_id("b/a/f.txt"), file_id)
        await self.assert_rescan_is_quiet()

    async def test_move_into_own_subtree_is_invalid(self) -> None:
        # given
        (self.source / "a/inner").mkdir()
        await self.scan()
        folder = await self.node("a")
        assert folder is not None

        # when / then
        with self.assertRaises(OperationInvalidError):
            await self.operations.update(
                folder, name=None, parent_id=await self.node_id("a/inner")
            )

    async def test_stale_node_is_not_renamed(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")
        self.write("a/f.txt", b"someone else's file")

        # when / then
        with self.assertRaises(OperationUnavailableError):
            await self.operations.update(record, name="g.txt", parent_id=None)

        # then
        self.assertTrue((self.source / "a/f.txt").exists())


class TestDelete(_WriteTestCase):
    async def test_deletes_file_permanently(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None

        # when
        await self.operations.delete(record)

        # then
        self.assertFalse((self.source / "a/f.txt").exists())
        self.assertIsNone(await self.mirror.get_node_by_id(record.id))

    async def test_deletes_folder_with_contents(self) -> None:
        # given
        folder = await self.node("a")
        assert folder is not None
        file_id = await self.node_id("a/f.txt")

        # when
        await self.operations.delete(folder)

        # then
        self.assertFalse((self.source / "a").exists())
        self.assertIsNone(await self.mirror.get_node_by_id(file_id))

    async def test_replaced_entry_is_not_deleted(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")
        self.write("a/f.txt", b"someone else's file")

        # when / then
        with self.assertRaises(OperationUnavailableError):
            await self.operations.delete(record)

        # then
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"someone else's file")

    async def test_entry_already_gone_is_removed_from_mirror(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        os.unlink(self.source / "a/f.txt")

        # when
        await self.operations.delete(record)

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(record.id))


class TestUpload(_WriteTestCase):
    async def test_upload_creates_file(self) -> None:
        # when
        record = await self.uploader.upload(
            await self.node_id("b"),
            "new.bin",
            _content(b"part one ", b"part two"),
            mime_type=None,
            media_info=None,
        )

        # then
        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"part one part two")
        self.assertEqual(record.hash, hashlib.md5(b"part one part two").hexdigest())
        self.assertEqual(await self.node_id("b/new.bin"), record.id)
        self.assertEqual(self.staging_files(), [])
        await self.assert_rescan_is_quiet()

    async def test_upload_keeps_client_media_info(self) -> None:
        # when
        record = await self.uploader.upload(
            DOCS,
            "pic.jpg",
            _content(b"jpeg"),
            mime_type="image/jpeg",
            media_info=MediaInfo(
                width=4, height=3, ms_duration=0, is_image=True, is_video=False
            ),
        )
        # then
        self.assertEqual((record.width, record.height), (4, 3))

    async def test_upload_onto_existing_name_conflicts(self) -> None:
        # given
        existing = await self.node_id("a/f.txt")

        # when / then
        with self.assertRaises(UploadConflictError) as cm:
            await self.uploader.upload(
                await self.node_id("a"),
                "f.txt",
                _content(b"replacement"),
                mime_type=None,
                media_info=None,
            )

        # then
        self.assertEqual(cm.exception.record.id, existing)
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"hello")
        self.assertEqual(self.staging_files(), [])

    async def test_find_conflict_reports_existing_node(self) -> None:
        # when
        conflict = await self.uploader.find_conflict(await self.node_id("a"), "f.txt")
        # then
        assert conflict is not None
        self.assertEqual(conflict.id, await self.node_id("a/f.txt"))
        self.assertIsNone(await self.uploader.find_conflict(DOCS, "free.txt"))

    async def test_interrupted_upload_leaves_nothing(self) -> None:
        # when / then
        with self.assertRaises(ConnectionResetError):
            await self.uploader.upload(
                DOCS, "broken.bin", _failing_content(), mime_type=None, media_info=None
            )

        # then
        self.assertFalse((self.source / "broken.bin").exists())
        self.assertEqual(self.staging_files(), [])
        self.assertIsNone(await self.node("broken.bin"))

    async def test_staging_files_are_not_mirrored(self) -> None:
        # given
        self.write(f"b/{STAGING_PREFIX}leftover", b"x")
        # when
        await self.scan()
        # then
        children = await self.mirror.get_children(await self.node_id("b"))
        self.assertEqual(children, [])


class TestResumableUpload(_WriteTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.files: list[FilesystemFile] = []

    async def asyncTearDown(self) -> None:
        # Before the harness stops the pool the files are closed on.
        for file in self.files:
            await file.abort()
        await super().asyncTearDown()

    async def open(self, parent: str, name: str) -> FilesystemFile:
        file = await self.uploader.open(
            await self.node_id(parent), name, 0, mime_type=None, media_info=None
        )
        self.files.append(file)
        return file

    async def write_at(self, file: FilesystemFile, offset: int, data: bytes) -> None:
        await file.seek(offset)
        self.assertEqual(await file.write(data), len(data))

    async def test_file_is_published_by_close(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"part one ")
        await self.write_at(file, 9, b"part two")
        self.assertEqual(len(self.staging_files()), 1)

        # when
        record = await file.close()

        # then
        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"part one part two")
        self.assertEqual(record.hash, hashlib.md5(b"part one part two").hexdigest())
        self.assertEqual(await self.node_id("b/new.bin"), record.id)
        self.assertEqual(self.staging_files(), [])
        await self.assert_rescan_is_quiet()

    async def test_rewritten_range_keeps_the_checksum(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"abc")
        await self.write_at(file, 0, b"abc")
        await self.write_at(file, 3, b"def")

        # when
        record = await file.close()

        # then
        self.assertEqual(record.hash, hashlib.md5(b"abcdef").hexdigest())

    async def test_range_rewritten_with_other_bytes_is_rehashed(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"abc")
        await self.write_at(file, 0, b"xyz")
        await self.write_at(file, 3, b"def")

        # when
        record = await file.close()

        # then
        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"xyzdef")
        self.assertEqual(record.hash, hashlib.md5(b"xyzdef").hexdigest())

    async def test_close_after_a_failed_record_finishes_it(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        record_once = self.uploader._record
        failures = [OSError("mirror unavailable")]

        async def flaky(*args: Any, **kwargs: Any) -> NodeRecord:
            if failures:
                raise failures.pop()
            return await record_once(*args, **kwargs)

        with patch.object(self.uploader, "_record", flaky):
            with self.assertRaises(OSError):
                await file.close()
            self.assertEqual((self.source / "b/new.bin").read_bytes(), b"data")

            # when
            record = await file.close()

        # then
        self.assertEqual(await self.node_id("b/new.bin"), record.id)
        await file.abort()
        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"data")
        self.assertEqual(self.staging_files(), [])
        await self.assert_rescan_is_quiet()

    async def test_close_after_a_scan_keeps_the_scanned_node(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        with patch.object(
            self.uploader, "_record", side_effect=OSError("mirror unavailable")
        ):
            with self.assertRaises(OSError):
                await file.close()
        await self.scan()
        scanned = await self.node_id("b/new.bin")

        # when
        record = await file.close()

        # then
        self.assertEqual(record.id, scanned)
        children = await self.mirror.get_children(await self.node_id("b"))
        self.assertEqual([c.name for c in children], ["new.bin"])
        await self.assert_rescan_is_quiet()

    async def test_abort_after_a_failed_record_keeps_the_published_file(
        self,
    ) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        with patch.object(
            self.uploader, "_record", side_effect=OSError("mirror unavailable")
        ):
            with self.assertRaises(OSError):
                await file.close()

        # when
        await file.abort()

        # then
        self.assertEqual((self.source / "b/new.bin").read_bytes(), b"data")

    async def test_close_follows_a_moved_folder(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        os.rename(self.source / "b", self.source / "a/b-moved")
        await self.scan()

        # when
        record = await file.close()

        # then
        self.assertEqual((self.source / "a/b-moved/new.bin").read_bytes(), b"data")
        self.assertEqual(await self.node_id("a/b-moved/new.bin"), record.id)

    async def test_folder_moved_before_a_scan_is_transient(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        os.rename(self.source / "b", self.source / "b-moved")

        # when / then
        with self.assertRaises(UploadTransientError):
            await file.close()

        # then: nothing was published, and the staged bytes are kept for a retry
        self.assertEqual(list((self.source / "b-moved").glob("new.bin")), [])
        await self.scan()
        record = await file.close()
        self.assertEqual(await self.node_id("b-moved/new.bin"), record.id)

    async def test_deleted_folder_is_parent_gone(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")
        shutil.rmtree(self.source / "b")
        await self.scan()

        # when / then
        with self.assertRaises(UploadParentGoneError):
            await file.close()

    async def test_conflict_on_close_keeps_the_existing_file(self) -> None:
        # given
        file = await self.open("a", "f.txt")
        await self.write_at(file, 0, b"replacement")

        # when / then
        with self.assertRaises(UploadConflictError) as cm:
            await file.close()

        # then
        self.assertEqual(cm.exception.record.id, await self.node_id("a/f.txt"))
        self.assertEqual((self.source / "a/f.txt").read_bytes(), b"hello")
        await file.abort()
        self.assertEqual(self.staging_files(), [])

    async def test_abort_removes_the_staging_file(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        await self.write_at(file, 0, b"data")

        # when
        await file.abort()
        await file.abort()

        # then
        self.assertEqual(self.staging_files(), [])
        self.assertIsNone(await self.node("b/new.bin"))

    async def test_abort_finds_the_file_in_a_moved_folder(self) -> None:
        # given
        file = await self.open("b", "new.bin")
        os.rename(self.source / "b", self.source / "b-moved")
        await self.scan()

        # when
        await file.abort()

        # then
        self.assertEqual(self.staging_files(), [])


class TestFolderTimes(_WriteTestCase):
    """A write records the new times of the folders it changed."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Far from now, so a stale record differs even in whole seconds.
        for folder in ("a", "b"):
            os.utime(self.source / folder, (1_000_000, 1_000_000))
        await self.scan()

    async def assert_folder_is_current(self, relative: str) -> None:
        record = await self.node(relative)
        assert record is not None
        st = os.stat(self.source / relative)
        self.assertEqual(record.modified_time, int(st.st_mtime))
        self.assertEqual(record.changed_time, int(st.st_ctime))

    async def test_create_folder(self) -> None:
        # when
        await self.operations.create_folder(await self.node_id("a"), "new")
        # then
        await self.assert_folder_is_current("a")
        await self.assert_rescan_is_quiet()

    async def test_upload(self) -> None:
        # when
        await self.uploader.upload(
            await self.node_id("b"),
            "new.bin",
            _content(b"x"),
            mime_type=None,
            media_info=None,
        )
        # then
        await self.assert_folder_is_current("b")
        await self.assert_rescan_is_quiet()

    async def test_rename(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        # when
        await self.operations.update(record, name="g.txt", parent_id=None)
        # then
        await self.assert_folder_is_current("a")
        await self.assert_rescan_is_quiet()

    async def test_move_records_both_folders(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        # when
        await self.operations.update(
            record, name=None, parent_id=await self.node_id("b")
        )
        # then
        await self.assert_folder_is_current("a")
        await self.assert_folder_is_current("b")
        await self.assert_rescan_is_quiet()

    async def test_delete(self) -> None:
        # given
        record = await self.node("a/f.txt")
        assert record is not None
        # when
        await self.operations.delete(record)
        # then
        await self.assert_folder_is_current("a")
        await self.assert_rescan_is_quiet()


class TestWritesWithWatcher(_WriteTestCase):
    watch = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if self.watcher is None:
            self.skipTest("inotify is not available")

    async def test_watched_write_is_not_mirrored_twice(self) -> None:
        # given
        assert self.watcher is not None
        self.watcher.take()
        # when
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

        # then
        root_names = [c.name for c in await self.mirror.get_children(DOCS)]
        self.assertEqual(root_names.count("fresh"), 1)
        self.assertEqual(await self.node_id("fresh"), created.id)
        self.assertEqual(await self.node_id("fresh/f.txt"), uploaded.id)

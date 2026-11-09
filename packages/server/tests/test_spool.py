"""Tests for spooling resumable uploads in front of a one-shot uploader."""

import fcntl
import os
import tempfile
import time
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.services.spool import (
    SpooledFile,
    SpooledFiles,
    create_spool_area,
    sweep_spool_areas,
)
from wcpan.drive.server.services.upload import (
    UploadPermanentError,
    UploadTransientError,
)


_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class _RecordingUploader:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.calls: list[tuple[NodeId, str, str | None, MediaInfo | None]] = []
        self.error: Exception | None = None

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        return None

    async def upload(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        self.calls.append((parent_id, name, mime_type, media_info))
        self.received.append(b"".join([chunk async for chunk in content]))
        if self.error is not None:
            raise self.error
        return NodeRecord(
            id=NodeId("new"),
            parent_id=parent_id,
            name=name,
            is_directory=False,
            created_time=_EPOCH,
            modified_time=_EPOCH,
            changed_time=_EPOCH,
            mime_type=mime_type or "",
            hash="",
            hash_algorithm="",
            size=0,
            is_image=False,
            is_video=False,
            width=0,
            height=0,
            ms_duration=0,
        )


class TestSpooledFiles(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.area = self.enterContext(create_spool_area(tmp_dir=self.tmp))
        self.uploader = _RecordingUploader()
        self.files = SpooledFiles(uploader=self.uploader, area=self.area)

    async def open(
        self,
        size: int,
        *,
        mime_type: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> SpooledFile:
        file = await self.files.open(
            NodeId("p"), "f.bin", size, mime_type=mime_type, media_info=media_info
        )
        self.addAsyncCleanup(file.abort)
        return file

    async def test_open_creates_a_file_in_the_area(self) -> None:
        # when
        file = await self.open(4)
        # then
        self.assertTrue(file.path.is_relative_to(self.area.directory))
        self.assertTrue(file.path.exists())

    async def test_write_keeps_everything(self) -> None:
        # given
        file = await self.open(6)
        # when
        kept = [await file.write(0, b"abc"), await file.write(3, b"def")]
        # then
        self.assertEqual(kept, [3, 3])
        self.assertEqual(self.uploader.received, [])
        await file.close()
        self.assertEqual(self.uploader.received, [b"abcdef"])

    async def test_close_streams_the_spooled_bytes(self) -> None:
        # given
        media = MediaInfo(width=1, height=2, ms_duration=0, is_image=True)
        file = await self.open(6, mime_type="image/png", media_info=media)
        await file.write(0, b"abcdef")
        # when
        record = await file.close()
        # then
        self.assertEqual(record.name, "f.bin")
        self.assertEqual(self.uploader.received, [b"abcdef"])
        self.assertEqual(
            self.uploader.calls, [(NodeId("p"), "f.bin", "image/png", media)]
        )
        self.assertFalse(file.path.exists())

    async def test_rewrite_overwrites(self) -> None:
        # given
        file = await self.open(6)
        await file.write(0, b"abc")
        # when
        await file.write(0, b"xyz")
        await file.write(3, b"def")
        await file.close()
        # then
        self.assertEqual(self.uploader.received, [b"xyzdef"])

    async def test_short_file_is_not_uploaded(self) -> None:
        # given
        file = await self.open(6)
        await file.write(0, b"abc")
        # when / then
        with self.assertRaises(UploadPermanentError):
            await file.close()
        # then
        self.assertEqual(self.uploader.calls, [])

    async def test_failed_upload_keeps_the_file_for_a_retry(self) -> None:
        # given
        file = await self.open(3)
        await file.write(0, b"abc")
        self.uploader.error = UploadTransientError("later")
        with self.assertRaises(UploadTransientError):
            await file.close()
        self.uploader.error = None
        # when
        await file.close()
        # then
        self.assertEqual(self.uploader.received, [b"abc", b"abc"])

    async def test_abort_removes_the_file_and_is_idempotent(self) -> None:
        # given
        file = await self.open(3)
        # when
        await file.abort()
        await file.abort()
        # then
        self.assertFalse(file.path.exists())
        with self.assertRaises(UploadPermanentError):
            await file.write(0, b"abc")


class TestSpoolAreas(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def area_dir(self, name: str, *, locked_file: bool = True) -> Path:
        path = self.tmp / name
        path.mkdir()
        (path / "wcpan_upload_1.tmp").write_bytes(b"x")
        if locked_file:
            (path / ".lock").touch()
        return path

    def test_area_is_removed_on_exit(self) -> None:
        # given
        with create_spool_area(tmp_dir=self.tmp) as area:
            self.assertTrue(area.directory.is_dir())
        # then
        self.assertFalse(area.directory.exists())

    def test_abandoned_area_is_removed(self) -> None:
        # given
        orphan = self.area_dir("wcpan_upload_orphan")
        # when
        with create_spool_area(tmp_dir=self.tmp) as area:
            # then
            self.assertFalse(orphan.exists())
            self.assertTrue(area.directory.exists())

    def test_live_area_is_kept(self) -> None:
        # given
        with create_spool_area(tmp_dir=self.tmp) as live:
            # when
            with create_spool_area(tmp_dir=self.tmp):
                # then
                self.assertTrue(live.directory.exists())

    def test_area_locked_elsewhere_is_kept(self) -> None:
        # given
        held = self.area_dir("wcpan_upload_held")
        fd = os.open(held / ".lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        # when
        sweep_spool_areas(self.tmp)
        # then
        self.assertTrue(held.exists())

    def test_unlocked_area_is_removed_only_when_old(self) -> None:
        # given
        young = self.area_dir("wcpan_upload_young", locked_file=False)
        old = self.area_dir("wcpan_upload_old", locked_file=False)
        then = time.time() - 2 * 3600
        os.utime(old, (then, then))
        # when
        sweep_spool_areas(self.tmp)
        # then
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())

    def test_other_entries_are_untouched(self) -> None:
        # given
        other = self.area_dir("other")
        stray = self.tmp / "wcpan_upload_file"
        stray.write_bytes(b"x")
        # when
        sweep_spool_areas(self.tmp)
        # then
        self.assertTrue(other.exists())
        self.assertTrue(stray.exists())

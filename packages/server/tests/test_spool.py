"""Tests for spooling resumable uploads in front of a one-shot uploader."""

import tempfile
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.services.spool import (
    SpooledFile,
    SpooledFiles,
    create_spool_area,
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

    async def test_write_keeps_everything_and_advances(self) -> None:
        # given
        file = await self.open(6)
        # when
        kept = [await file.write(b"abc"), await file.write(b"def")]
        await file.flush()
        # then
        self.assertEqual(kept, [3, 3])
        self.assertEqual(file.path.read_bytes(), b"abcdef")

    async def test_close_streams_the_spooled_bytes(self) -> None:
        # given
        media = MediaInfo(width=1, height=2, ms_duration=0, is_image=True)
        file = await self.open(6, mime_type="image/png", media_info=media)
        await file.write(b"abcdef")
        # when
        record = await file.close()
        # then
        self.assertEqual(record.name, "f.bin")
        self.assertEqual(self.uploader.received, [b"abcdef"])
        self.assertEqual(
            self.uploader.calls, [(NodeId("p"), "f.bin", "image/png", media)]
        )
        self.assertFalse(file.path.exists())

    async def test_seek_back_overwrites(self) -> None:
        # given
        file = await self.open(6)
        await file.write(b"abc")
        # when
        await file.seek(0)
        await file.write(b"xyz")
        await file.write(b"def")
        await file.close()
        # then
        self.assertEqual(self.uploader.received, [b"xyzdef"])

    async def test_short_file_is_not_uploaded(self) -> None:
        # given
        file = await self.open(6)
        await file.write(b"abc")
        # when / then
        with self.assertRaises(UploadPermanentError):
            await file.close()
        # then
        self.assertEqual(self.uploader.calls, [])

    async def test_failed_upload_keeps_the_file_for_a_retry(self) -> None:
        # given
        file = await self.open(3)
        await file.write(b"abc")
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
            await file.write(b"abc")

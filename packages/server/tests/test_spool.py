"""Tests for spooling resumable uploads in front of a one-shot uploader."""

import tempfile
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.services.spool import (
    SpoolStage,
    SpoolStaging,
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


class TestSpoolStaging(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.area = self.enterContext(create_spool_area(tmp_dir=self.tmp))
        self.uploader = _RecordingUploader()
        self.staging = SpoolStaging(uploader=self.uploader, area=self.area)

    async def begin(
        self,
        total: int,
        *,
        mime_type: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> SpoolStage:
        stage = await self.staging.begin(
            NodeId("p"), "f.bin", total, mime_type=mime_type, media_info=media_info
        )
        self.addAsyncCleanup(stage.abort)
        return stage

    async def test_begin_creates_a_file_in_the_area(self) -> None:
        # when
        stage = await self.begin(4)
        # then
        self.assertTrue(stage.path.is_relative_to(self.area.directory))
        self.assertTrue(stage.path.exists())

    async def test_commit_streams_the_spooled_bytes(self) -> None:
        # given
        media = MediaInfo(width=1, height=2, ms_duration=0, is_image=True)
        stage = await self.begin(6, mime_type="image/png", media_info=media)
        await stage.write_at(0, b"abc")
        await stage.write_at(3, b"def")
        # when
        record = await stage.commit()
        # then
        self.assertEqual(record.name, "f.bin")
        self.assertEqual(self.uploader.received, [b"abcdef"])
        self.assertEqual(
            self.uploader.calls, [(NodeId("p"), "f.bin", "image/png", media)]
        )
        self.assertFalse(stage.path.exists())

    async def test_rewritten_range_is_not_duplicated(self) -> None:
        # given
        stage = await self.begin(6)
        await stage.write_at(0, b"abc")
        await stage.write_at(0, b"abc")
        await stage.write_at(3, b"def")
        # when
        await stage.commit()
        # then
        self.assertEqual(self.uploader.received, [b"abcdef"])

    async def test_short_file_is_not_uploaded(self) -> None:
        # given
        stage = await self.begin(6)
        await stage.write_at(0, b"abc")
        # when / then
        with self.assertRaises(UploadPermanentError):
            await stage.commit()
        # then
        self.assertEqual(self.uploader.calls, [])

    async def test_failed_upload_keeps_the_file_for_a_retry(self) -> None:
        # given
        stage = await self.begin(3)
        await stage.write_at(0, b"abc")
        self.uploader.error = UploadTransientError("later")
        with self.assertRaises(UploadTransientError):
            await stage.commit()
        self.uploader.error = None
        # when
        await stage.commit()
        # then
        self.assertEqual(self.uploader.received, [b"abc", b"abc"])

    async def test_abort_removes_the_file_and_is_idempotent(self) -> None:
        # given
        stage = await self.begin(3)
        # when
        await stage.abort()
        await stage.abort()
        # then
        self.assertFalse(stage.path.exists())
        with self.assertRaises(UploadPermanentError):
            await stage.write_at(0, b"abc")

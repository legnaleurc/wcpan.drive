"""Tests for server._services.upload session store."""

import asyncio
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.services.upload import (
    UploadBusyError,
    UploadCreatedNode,
    UploadService,
    UploadSessionNotFoundError,
    UploadSessionStore,
)


_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class TestUploadSessionStore(TestCase):
    def test_create_returns_session_with_correct_fields(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            media = MediaInfo(
                width=100, height=0, ms_duration=0, is_image=True, is_video=False
            )
            # when
            session = service.create(
                parent_id="p1",
                name="file.bin",
                total_size=1024,
                mime_type="application/octet-stream",
                media_info=media,
            )
            # then
            self.assertEqual(session.parent_id, "p1")
            self.assertEqual(session.name, "file.bin")
            self.assertEqual(session.total_size, 1024)
            self.assertEqual(session.mime_type, "application/octet-stream")
            self.assertEqual(session.media_info, media)
            self.assertEqual(session.received, 0)
            self.assertTrue(session.temp_path.exists())

    def test_create_places_files_in_tmp_dir(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            # when
            session = service.create("p", "f", 10, None, None)
            # then
            self.assertTrue(session.temp_path.is_relative_to(td))

    def test_get_returns_created_session(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            session = service.create("p", "f", 10, None, None)
            # when
            result = service.get(session.session_id)
            # then
            self.assertIs(result, session)

    def test_get_missing_returns_none(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            # when / then
            self.assertIsNone(service.get("nonexistent"))

    def test_remove_returns_session_and_makes_it_unreachable(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            session = service.create("p", "f", 10, None, None)
            # when
            removed = service.remove(session.session_id)
            # then
            self.assertIs(removed, session)
            self.assertIsNone(service.get(session.session_id))

    def test_remove_missing_returns_none(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            # when / then
            self.assertIsNone(service.remove("nonexistent"))

    def test_close_all_clears_sessions(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            s1 = service.create("p", "a", 10, None, None)
            s2 = service.create("p", "b", 20, None, None)
            # when
            service.close_all()
            # then
            self.assertIsNone(service.get(s1.session_id))
            self.assertIsNone(service.get(s2.session_id))

    def test_session_ids_are_unique(self):
        # given
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            # when
            sessions = [service.create("p", "f", 10, None, None) for _ in range(10)]
            # then
            ids = {s.session_id for s in sessions}
            self.assertEqual(len(ids), 10)


class _GatedUploader:
    """One-shot uploader that waits for ``gate`` before finishing."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.uploads: list[bytes] = []

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
        self.started.set()
        self.uploads.append(b"".join([chunk async for chunk in content]))
        await self.gate.wait()
        return NodeRecord(
            id=NodeId("new"),
            parent_id=parent_id,
            name=name,
            is_directory=False,
            created_time=_EPOCH,
            modified_time=_EPOCH,
            changed_time=_EPOCH,
            mime_type="",
            hash="",
            hash_algorithm="",
            size=0,
            is_image=False,
            is_video=False,
            width=0,
            height=0,
            ms_duration=0,
        )


class TestDeleteDuringUpload(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        td = self.enterContext(tempfile.TemporaryDirectory())
        self.store = UploadSessionStore(tmp_dir=Path(td))
        self.uploader = _GatedUploader()
        self.service = UploadService(store=self.store, uploader=self.uploader)

    async def test_delete_while_writing_cancels_before_publishing(self) -> None:
        # given
        session = self.store.create("p", "f.bin", 6, None, None)
        sent = asyncio.Event()
        resume = asyncio.Event()

        async def slow() -> AsyncIterator[bytes]:
            yield b"abc"
            sent.set()
            await resume.wait()
            yield b"def"

        writer = asyncio.create_task(
            self.service.append_chunk(session.session_id, 0, slow())
        )
        await sent.wait()
        # when
        deleted = self.service.delete_session(session.session_id)
        resume.set()
        # then
        with self.assertRaises(UploadSessionNotFoundError):
            await writer
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get(session.session_id))
        self.assertEqual(self.uploader.uploads, [])
        self.assertFalse(session.temp_path.exists())

    async def test_delete_while_publishing_is_refused(self) -> None:
        # given
        session = self.store.create("p", "f.bin", 3, None, None)

        async def body() -> AsyncIterator[bytes]:
            yield b"abc"

        writer = asyncio.create_task(
            self.service.append_chunk(session.session_id, 0, body())
        )
        await self.uploader.started.wait()
        # when / then
        with self.assertRaises(UploadBusyError):
            self.service.delete_session(session.session_id)
        # then
        self.uploader.gate.set()
        self.assertIsInstance(await writer, UploadCreatedNode)
        self.assertEqual(self.uploader.uploads, [b"abc"])
        self.assertFalse(session.temp_path.exists())

"""Tests for the resumable upload session service."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.services.upload import (
    SessionState,
    UploadAccepted,
    UploadBusyError,
    UploadConflictError,
    UploadCreatedNode,
    UploadInvalidChunkError,
    UploadOffsetMismatchError,
    UploadService,
    UploadSessionNotFoundError,
    UploadSessionStore,
    UploadTransientError,
)


_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _record(name: str) -> NodeRecord:
    return NodeRecord(
        id=NodeId(f"id-{name}"),
        parent_id=NodeId("p1"),
        name=name,
        is_directory=False,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type="application/octet-stream",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class _FakeStage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.data = bytearray()
        self.writes: list[tuple[int, bytes]] = []
        self.commits = 0
        self.aborts = 0
        self.commit_error: Exception | None = None
        self.commit_gate: asyncio.Event | None = None

    async def write_at(self, offset: int, data: bytes) -> None:
        self.writes.append((offset, data))
        self.data[offset : offset + len(data)] = data

    async def commit(self) -> NodeRecord:
        self.commits += 1
        if self.commit_gate is not None:
            await self.commit_gate.wait()
        if self.commit_error is not None:
            raise self.commit_error
        return _record(self.name)

    async def abort(self) -> None:
        self.aborts += 1


class _FakeBackend:
    """Uploader and UploadStaging over in-memory stages."""

    def __init__(self) -> None:
        self.conflict: NodeRecord | None = None
        self.stages: list[_FakeStage] = []
        self.begun: list[tuple[NodeId, str, int, str | None, MediaInfo | None]] = []

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        return self.conflict

    async def upload(self, *args: object, **kwargs: object) -> NodeRecord:
        raise AssertionError("direct upload is not used here")

    async def begin(
        self,
        parent_id: NodeId,
        name: str,
        total_size: int,
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> _FakeStage:
        self.begun.append((parent_id, name, total_size, mime_type, media_info))
        stage = _FakeStage(name)
        self.stages.append(stage)
        return stage


async def _content(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _ServiceTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = _FakeBackend()
        self.store = UploadSessionStore()
        self.service = UploadService(
            store=self.store, uploader=self.backend, staging=self.backend
        )

    async def create(self, total: int, name: str = "f.bin") -> str:
        created = await self.service.create_session(
            NodeId("p1"), name, total, None, None
        )
        return created.session_id

    def stage(self) -> _FakeStage:
        return self.backend.stages[-1]


class TestCreateSession(_ServiceTestCase):
    async def test_begins_a_stage_with_normalized_name(self) -> None:
        # given
        media = MediaInfo(
            width=100, height=0, ms_duration=0, is_image=True, is_video=False
        )
        # when
        created = await self.service.create_session(
            NodeId("p1"), "が.bin", 1024, "image/png", media
        )
        # then
        self.assertEqual(
            self.backend.begun, [(NodeId("p1"), "が.bin", 1024, "image/png", media)]
        )
        session = self.store.get(created.session_id)
        assert session is not None
        self.assertEqual(session.received, 0)
        self.assertIs(session.stage, self.stage())

    async def test_conflict_begins_nothing(self) -> None:
        # given
        self.backend.conflict = _record("f.bin")
        # when / then
        with self.assertRaises(UploadConflictError):
            await self.create(10)
        # then
        self.assertEqual(self.backend.stages, [])

    async def test_session_ids_are_unique(self) -> None:
        # when
        ids = {await self.create(10, f"f{i}") for i in range(10)}
        # then
        self.assertEqual(len(ids), 10)


class TestAppendChunk(_ServiceTestCase):
    async def test_partial_chunk_is_written_at_the_received_offset(self) -> None:
        # given
        session_id = await self.create(10)
        await self.service.append_chunk(session_id, 0, _content(b"abc"))
        # when
        result = await self.service.append_chunk(session_id, 3, _content(b"de"))
        # then
        self.assertEqual(result, UploadAccepted(offset=5))
        self.assertEqual(self.stage().writes, [(0, b"abc"), (3, b"de")])

    async def test_writes_are_batched(self) -> None:
        # given
        session_id = await self.create(10)
        # when
        with patch("wcpan.drive.server.services.upload.FILE_CHUNK_SIZE", 4):
            await self.service.append_chunk(session_id, 0, _content(b"abc", b"def"))
        # then
        self.assertEqual(self.stage().writes, [(0, b"abcd"), (4, b"ef")])

    async def test_wrong_offset_reports_received(self) -> None:
        # given
        session_id = await self.create(10)
        await self.service.append_chunk(session_id, 0, _content(b"abc"))
        # when / then
        with self.assertRaises(UploadOffsetMismatchError) as cm:
            await self.service.append_chunk(session_id, 0, _content(b"abc"))
        # then
        self.assertEqual(cm.exception.offset, 3)

    async def test_overlong_body_is_rejected(self) -> None:
        # given
        session_id = await self.create(4)
        # when / then
        with self.assertRaises(UploadInvalidChunkError):
            await self.service.append_chunk(session_id, 0, _content(b"abcde"))

    async def test_broken_stream_keeps_the_batches_written(self) -> None:
        # given
        session_id = await self.create(10)

        async def broken() -> AsyncIterator[bytes]:
            yield b"abcdef"
            raise ConnectionResetError()

        # when
        with patch("wcpan.drive.server.services.upload.FILE_CHUNK_SIZE", 4):
            with self.assertRaises(ConnectionResetError):
                await self.service.append_chunk(session_id, 0, broken())
        # then
        status = self.service.get_session_status(session_id)
        assert status is not None
        self.assertEqual(status.received, 4)
        session = self.store.get(session_id)
        assert session is not None
        self.assertIs(session.state, SessionState.IDLE)

    async def test_final_chunk_commits_and_removes_the_session(self) -> None:
        # given
        session_id = await self.create(3)
        # when
        result = await self.service.append_chunk(session_id, 0, _content(b"abc"))
        # then
        self.assertEqual(result, UploadCreatedNode(record=_record("f.bin")))
        self.assertIsNone(self.store.get(session_id))
        self.assertEqual(self.stage().aborts, 0)

    async def test_failed_commit_keeps_the_session_for_a_retry(self) -> None:
        # given
        session_id = await self.create(3)
        self.stage().commit_error = UploadTransientError("try later")
        with self.assertRaises(UploadTransientError):
            await self.service.append_chunk(session_id, 0, _content(b"abc"))
        self.stage().commit_error = None
        # when
        result = await self.service.append_chunk(session_id, 3, _content())
        # then
        self.assertIsInstance(result, UploadCreatedNode)
        self.assertEqual(self.stage().commits, 2)

    async def test_conflict_on_commit_discards_the_session(self) -> None:
        # given
        session_id = await self.create(3)
        self.stage().commit_error = UploadConflictError(_record("f.bin"))
        # when / then
        with self.assertRaises(UploadConflictError):
            await self.service.append_chunk(session_id, 0, _content(b"abc"))
        # then
        self.assertIsNone(self.store.get(session_id))
        self.assertEqual(self.stage().aborts, 1)


class TestDeleteSession(_ServiceTestCase):
    async def test_idle_session_is_aborted(self) -> None:
        # given
        session_id = await self.create(10)
        # when
        deleted = await self.service.delete_session(session_id)
        # then
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get(session_id))
        self.assertEqual(self.stage().aborts, 1)

    async def test_unknown_session(self) -> None:
        # when / then
        self.assertFalse(await self.service.delete_session("nonexistent"))

    async def test_writing_session_is_aborted_by_its_writer(self) -> None:
        # given
        session_id = await self.create(10)
        sent = asyncio.Event()
        resume = asyncio.Event()

        async def slow() -> AsyncIterator[bytes]:
            yield b"abc"
            sent.set()
            await resume.wait()
            yield b"defghij"

        with patch("wcpan.drive.server.services.upload.FILE_CHUNK_SIZE", 3):
            writer = asyncio.create_task(
                self.service.append_chunk(session_id, 0, slow())
            )
            await sent.wait()
            # when
            deleted = await self.service.delete_session(session_id)
            resume.set()
            # then
            with self.assertRaises(UploadSessionNotFoundError):
                await writer
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get(session_id))
        self.assertEqual(self.stage().writes, [(0, b"abc")])
        self.assertEqual(self.stage().commits, 0)
        self.assertEqual(self.stage().aborts, 1)

    async def test_committing_session_cannot_be_deleted(self) -> None:
        # given
        session_id = await self.create(3)
        gate = asyncio.Event()
        self.stage().commit_gate = gate
        writer = asyncio.create_task(
            self.service.append_chunk(session_id, 0, _content(b"abc"))
        )
        while self.stage().commits == 0:
            await asyncio.sleep(0)
        # when / then
        with self.assertRaises(UploadBusyError):
            await self.service.delete_session(session_id)
        # then
        gate.set()
        self.assertIsInstance(await writer, UploadCreatedNode)
        self.assertEqual(self.stage().aborts, 0)


class TestExpiry(_ServiceTestCase):
    async def test_idle_sessions_past_ttl_are_aborted(self) -> None:
        # given
        session_id = await self.create(10)
        session = self.store.get(session_id)
        assert session is not None
        session.last_activity = 10.0
        # when
        removed = await self.service.remove_expired(now=3610.0, ttl=3600.0)
        # then
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get(session_id))
        self.assertEqual(self.stage().aborts, 1)

    async def test_writing_sessions_are_kept(self) -> None:
        # given
        session_id = await self.create(10)
        session = self.store.get(session_id)
        assert session is not None
        session.last_activity = 10.0
        session.state = SessionState.WRITING
        # when
        removed = await self.service.remove_expired(now=10_000.0, ttl=3600.0)
        # then
        self.assertEqual(removed, 0)
        self.assertIsNotNone(self.store.get(session_id))

    async def test_close_aborts_idle_and_cancels_writing_sessions(self) -> None:
        # given
        idle_id = await self.create(10, "idle")
        idle = self.stage()
        writing_id = await self.create(10, "writing")
        writing = self.store.get(writing_id)
        assert writing is not None
        writing.state = SessionState.WRITING
        # when
        await self.service.close()
        # then
        self.assertIsNone(self.store.get(idle_id))
        self.assertIsNone(self.store.get(writing_id))
        self.assertEqual(idle.aborts, 1)
        self.assertTrue(writing.cancelled)

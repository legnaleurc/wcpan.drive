"""Integration tests for the client–server HTTP contract.

These tests spin up the real aiohttp handlers against a minimal in-process
app (fake Storage, stub OffMainService) and make actual HTTP requests via
aiohttp.test_utils.TestClient.  No real Synology connection is needed.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.client.remote._lib import node_record_from_dict
from wcpan.drive.client.remote.writable import _media_info_to_params
from wcpan.drive.client.types import MediaInfo
from wcpan.drive.protocol.types import MirrorMutableId, NodeRecord
from wcpan.drive.server.handlers.changes import get_root
from wcpan.drive.server.handlers.health import put_null
from wcpan.drive.server.handlers.nodes import upload_node
from wcpan.drive.server.handlers.upload import (
    create_upload_session,
    delete_upload_session,
    head_upload_session,
    patch_upload_chunk,
)
from wcpan.drive.server.keys import (
    CHANGE_SERVICE_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    STORAGE_KEY,
    SYNOLOGY_DRIVE_API_KEY,
    SYNOLOGY_PATH_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.lib.mounts import MountRegistry
from wcpan.drive.server.services.paths import (
    SERVER_ROOT_ID,
    SynologyPathService,
)
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadService,
    UploadSessionStore,
)
from wcpan.drive.server.workers import create_write_queue


_EPOCH = 0

# Fake Synology file metadata returned by the patched upload_file call.
_FAKE_SYNO_INFO = {
    "file_id": "syno-99",
    "permanent_link": "syno-99",
    "name": "photo.jpg",
    "type": "file",
    "content_type": "image",
    "size": 10,
    "created_time": 1_000_000,
    "modified_time": 1_000_000,
    "change_time": 0,
    "hash": "abc123",
}


class _FakeOffMain:
    """Synchronous passthrough — runs callables directly on the event loop thread."""

    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeStorage:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record


def _make_app(
    storage: _FakeStorage,
    session_store: UploadSessionStore,
) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    storage._nodes.setdefault(
        "parent-a",
        NodeRecord(
            id="parent-a",
            parent_id=None,
            name="parent-a",
            is_directory=True,
            created_time=_EPOCH,
            modified_time=_EPOCH,
            changed_time=_EPOCH,
            mime_type="application/x-directory",
            hash="",
            hash_algorithm="",
            size=0,
            is_image=False,
            is_video=False,
            width=0,
            height=0,
            ms_duration=0,
            mutable_id=MirrorMutableId("parent-a"),
        ),
    )
    syno_paths = SynologyPathService(
        registry=MountRegistry(mounts={}, root_ids={}),
        storage=storage,  # type: ignore[arg-type]
    )
    node_sync = NodeSyncService(
        storage=storage,
        write_queue=wq,
        off_main=off_main,
        mounts={},
        local_paths={},
        metadata_queue=asyncio.Queue(),
    )  # type: ignore[arg-type]
    drive_api = MagicMock()
    drive_api.list_folder = AsyncMock(return_value=([], 0))
    drive_api.get_file = AsyncMock(return_value=None)
    app[READY_KEY] = True
    app[STORAGE_KEY] = storage
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq
    app[SYNOLOGY_PATH_KEY] = syno_paths
    app[CHANGE_SERVICE_KEY] = node_sync
    app[SYNOLOGY_DRIVE_API_KEY] = drive_api
    app[UPLOAD_SERVICE_KEY] = UploadService(
        store=session_store,
        node_sync=node_sync,
        drive_api=drive_api,
        syno_paths=syno_paths,
    )

    app.router.add_get("/api/v1/root", get_root)
    app.router.add_put("/null", put_null)
    app.router.add_post("/api/v1/nodes/{parent_id}", upload_node)
    app.router.add_post("/api/v1/nodes/{parent_id}/uploads", create_upload_session)
    app.router.add_patch("/api/v1/uploads/{session_id}", patch_upload_chunk)
    app.router.add_head("/api/v1/uploads/{session_id}", head_upload_session)
    app.router.add_delete("/api/v1/uploads/{session_id}", delete_upload_session)
    return app


class TestMediaInfoContract(IsolatedAsyncioTestCase):
    """Client MediaInfo encoding must be correctly decoded by the server."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._session_store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._session_store.close_all()
        self._tmp.cleanup()

    async def test_upload_session_carries_media_info(self) -> None:
        """MediaInfo dimensions and flags survive the upload-session protocol."""
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)

        media = MediaInfo(
            is_image=True,
            is_video=False,
            width=1920,
            height=1080,
            ms_duration=0,
        )
        params = _media_info_to_params(media)

        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=_FAKE_SYNO_INFO,
        ):
            async with TestClient(TestServer(app)) as client:
                # Step 1 — create session
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "photo.jpg", "size": 10, **params},
                )
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                # Step 2 — send the single chunk; server finalises and returns 201
                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"x" * 10,
                    headers={"Upload-Offset": "0"},
                )
                self.assertEqual(resp2.status, 201)
                record = node_record_from_dict(await resp2.json())

        self.assertTrue(record.is_image)
        self.assertFalse(record.is_video)
        self.assertEqual(record.width, 1920)
        self.assertEqual(record.height, 1080)

    async def test_direct_upload_carries_media_info(self) -> None:
        """MediaInfo is applied when using the direct (non-session) upload endpoint."""
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)

        media = MediaInfo(
            is_image=True,
            is_video=False,
            width=640,
            height=480,
            ms_duration=0,
        )
        params = _media_info_to_params(media)

        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=_FAKE_SYNO_INFO,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/parent-a",
                    data=b"x" * 10,
                    params={"name": "photo.jpg", **params},
                )
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())

        self.assertTrue(record.is_image)
        self.assertEqual(record.width, 640)
        self.assertEqual(record.height, 480)

    async def test_video_media_info_roundtrip(self) -> None:
        """is_video and ms_duration survive the upload-session protocol."""
        storage = _FakeStorage()
        syno_info = {**_FAKE_SYNO_INFO, "content_type": "video", "name": "clip.mp4"}
        app = _make_app(storage, self._session_store)

        media = MediaInfo(
            is_image=False,
            is_video=True,
            width=1280,
            height=720,
            ms_duration=30_000,
        )
        params = _media_info_to_params(media)

        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=syno_info,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "clip.mp4", "size": 10, **params},
                )
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"v" * 10,
                    headers={"Upload-Offset": "0"},
                )
                self.assertEqual(resp2.status, 201)
                record = node_record_from_dict(await resp2.json())

        self.assertFalse(record.is_image)
        self.assertTrue(record.is_video)
        self.assertEqual(record.width, 1280)
        self.assertEqual(record.height, 720)
        self.assertEqual(record.ms_duration, 30_000)


class TestNodeRecordRoundTrip(IsolatedAsyncioTestCase):
    """NodeRecord JSON serialization must survive an HTTP round-trip."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._session_store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._session_store.close_all()
        self._tmp.cleanup()

    async def test_get_root_timestamps_preserved(self) -> None:
        """Timestamps are preserved through server JSON serialization."""
        storage = _FakeStorage()
        root = NodeRecord(
            id=SERVER_ROOT_ID,
            parent_id=None,
            name="root",
            is_directory=True,
            created_time=_EPOCH,
            modified_time=_EPOCH,
            changed_time=_EPOCH,
            mime_type="application/x-directory",
            hash="",
            hash_algorithm="",
            size=0,
            is_image=False,
            is_video=False,
            width=0,
            height=0,
            ms_duration=0,
            mutable_id=MirrorMutableId(""),
        )
        storage._nodes[SERVER_ROOT_ID] = root
        app = _make_app(storage, self._session_store)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/root")
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, SERVER_ROOT_ID)
        self.assertEqual(record.created_time, _EPOCH)
        self.assertEqual(record.modified_time, _EPOCH)


class TestNullUploadSink(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._session_store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._session_store.close_all()
        self._tmp.cleanup()

    async def test_put_null_returns_upload_stats(self) -> None:
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)
        payload = b"x" * 8192

        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/null", data=payload)
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertEqual(body["bytes_received"], len(payload))
        self.assertGreaterEqual(body["elapsed_seconds"], 0.0)
        self.assertGreaterEqual(body["bytes_per_second"], 0.0)
        self.assertGreaterEqual(body["mebibytes_per_second"], 0.0)


class TestUploadSessionProtocol(IsolatedAsyncioTestCase):
    """Multi-step upload session protocol: create → chunk → finalise."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._session_store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._session_store.close_all()
        self._tmp.cleanup()

    async def test_upload_returns_node_record(self) -> None:
        """PATCH with full file data returns 201 with a parseable NodeRecord."""
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)

        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=_FAKE_SYNO_INFO,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "photo.jpg", "size": 10},
                )
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"y" * 10,
                    headers={"Upload-Offset": "0"},
                )
                self.assertEqual(resp2.status, 201)
                record = node_record_from_dict(await resp2.json())

        self.assertEqual(record.id, "syno-99")
        self.assertEqual(record.name, "photo.jpg")

    async def test_partial_patch_returns_204(self) -> None:
        """PATCH with partial data returns 204 with updated Upload-Offset."""
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/nodes/parent-a/uploads",
                json={"name": "big.bin", "size": 20},
            )
            self.assertEqual(resp.status, 201)
            session_id = resp.headers["Location"].split("/")[-1]

            resp2 = await client.patch(
                f"/api/v1/uploads/{session_id}",
                data=b"a" * 10,
                headers={"Upload-Offset": "0"},
            )
            self.assertEqual(resp2.status, 204)
            self.assertEqual(resp2.headers["Upload-Offset"], "10")

    async def test_session_not_found_returns_404(self) -> None:
        storage = _FakeStorage()
        app = _make_app(storage, self._session_store)

        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/v1/uploads/no-such-session",
                data=b"x",
                headers={"Upload-Offset": "0"},
            )
            self.assertEqual(resp.status, 404)

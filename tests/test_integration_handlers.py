"""Integration tests for the client–server HTTP contract.

These tests spin up the real aiohttp handlers against a minimal in-process
app (fake MirrorStore, stub OffMainService) and make actual HTTP requests via
aiohttp.test_utils.TestClient.  No real Synology connection is needed.
"""

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.client.feed._lib import node_record_from_dict
from wcpan.drive.client.feed.writable import _media_info_to_params
from wcpan.drive.client.types import MediaInfo
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.backend import Mounts
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
    MIRROR_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.services.paths import SERVER_ROOT_ID
from wcpan.drive.server.services.spool import (
    SpoolArea,
    SpooledFiles,
    create_spool_area,
)
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadService,
    UploadSessionStore,
)
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._upload import SynologyUploader
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import create_write_queue


# Test-local slots for the Synology fakes; the backend no longer uses app keys.
SYNOLOGY_DRIVE_API_KEY: web.AppKey[Any] = web.AppKey("synology_drive_api")
SYNOLOGY_PATH_KEY: web.AppKey[Any] = web.AppKey("synology_path")


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


class _FakeMirror:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._locators: dict[str, SynologyLocator] = {}

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> set[str]:
        for o in observations:
            self._nodes[o.record.id] = o.record
            if o.locator is not None:
                self._locators[o.record.id] = o.locator
        return {o.record.id for o in observations}

    async def get_locators(self, node_ids: list[str]) -> dict[str, SynologyLocator]:
        # Fixture nodes use their own ID as the Synology file_id unless rebound.
        return {
            i: self._locators.get(i, SynologyLocator(i))
            for i in node_ids
            if i in self._nodes
        }


class _IdentityIds:
    """``NodeIdMap`` whose public IDs are the permanent links themselves."""

    async def public(self, natives: Sequence[str]) -> list[NodeId]:
        return [NodeId(n) for n in natives]

    async def lookup(self, native: str) -> NodeId | None:
        return NodeId(native)

    async def native_of(self, node_id: NodeId) -> str:
        return str(node_id)

    def mint(self) -> NodeId:
        raise AssertionError("Synology does not mint IDs")


def _make_app(
    mirror: _FakeMirror,
    spool: SpoolArea,
) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    mirror._nodes.setdefault(
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
        ),
    )
    syno_paths = SynologyPathService(
        registry=MountRegistry(mounts=Mounts.from_sources({}), root_ids={}),
        store=mirror,  # type: ignore[arg-type]
        ids=_IdentityIds(),
    )
    node_sync = NodeSyncService(
        mirror=mirror,
        write_queue=wq,
        off_main=off_main,
        mounts={},
        metadata_queue=asyncio.Queue(),
    )  # type: ignore[arg-type]
    drive_api = MagicMock()
    drive_api.list_folder = AsyncMock(return_value=([], 0))
    drive_api.get_file = AsyncMock(return_value=None)
    app[READY_KEY] = True
    app[MIRROR_KEY] = mirror
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq
    app[SYNOLOGY_PATH_KEY] = syno_paths
    app[CHANGE_SERVICE_KEY] = node_sync
    app[SYNOLOGY_DRIVE_API_KEY] = drive_api
    uploader = SynologyUploader(
        drive_api=drive_api,
        syno_paths=syno_paths,
        node_sync=node_sync,
    )
    app[UPLOAD_SERVICE_KEY] = UploadService(
        store=UploadSessionStore(),
        uploader=uploader,
        files=SpooledFiles(uploader=uploader, area=spool),
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
        self._spool = self.enterContext(create_spool_area(tmp_dir=Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_upload_session_carries_media_info(self) -> None:
        """MediaInfo dimensions and flags survive the upload-session protocol."""
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)

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
                # when
                # Step 1 — create session
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "photo.jpg", "size": 10, **params},
                )
                # then
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                # when
                # Step 2 — send the single chunk; server finalises and returns 201
                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"x" * 10,
                    headers={"Upload-Offset": "0"},
                )
                # then
                self.assertEqual(resp2.status, 201)
                record = node_record_from_dict(await resp2.json())

        self.assertTrue(record.is_image)
        self.assertFalse(record.is_video)
        self.assertEqual(record.width, 1920)
        self.assertEqual(record.height, 1080)

    async def test_direct_upload_carries_media_info(self) -> None:
        """MediaInfo is applied when using the direct (non-session) upload endpoint."""
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)

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
                # when
                resp = await client.post(
                    "/api/v1/nodes/parent-a",
                    data=b"x" * 10,
                    params={"name": "photo.jpg", **params},
                )
                # then
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())

        self.assertTrue(record.is_image)
        self.assertEqual(record.width, 640)
        self.assertEqual(record.height, 480)

    async def test_video_media_info_roundtrip(self) -> None:
        """is_video and ms_duration survive the upload-session protocol."""
        # given
        mirror = _FakeMirror()
        syno_info = {**_FAKE_SYNO_INFO, "content_type": "video", "name": "clip.mp4"}
        app = _make_app(mirror, self._spool)

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
                # when
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "clip.mp4", "size": 10, **params},
                )
                # then
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                # when
                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"v" * 10,
                    headers={"Upload-Offset": "0"},
                )
                # then
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
        self._spool = self.enterContext(create_spool_area(tmp_dir=Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_get_root_timestamps_preserved(self) -> None:
        """Timestamps are preserved through server JSON serialization."""
        # given
        mirror = _FakeMirror()
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
        )
        mirror._nodes[SERVER_ROOT_ID] = root
        app = _make_app(mirror, self._spool)

        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/root")
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, SERVER_ROOT_ID)
        self.assertEqual(record.created_time, _EPOCH)
        self.assertEqual(record.modified_time, _EPOCH)


class TestNullUploadSink(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._spool = self.enterContext(create_spool_area(tmp_dir=Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_put_null_returns_upload_stats(self) -> None:
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)
        payload = b"x" * 8192

        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.put("/null", data=payload)
            # then
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
        self._spool = self.enterContext(create_spool_area(tmp_dir=Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_upload_returns_node_record(self) -> None:
        """PATCH with full file data returns 201 with a parseable NodeRecord."""
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)

        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=_FAKE_SYNO_INFO,
        ):
            async with TestClient(TestServer(app)) as client:
                # when
                resp = await client.post(
                    "/api/v1/nodes/parent-a/uploads",
                    json={"name": "photo.jpg", "size": 10},
                )
                # then
                self.assertEqual(resp.status, 201)
                session_id = resp.headers["Location"].split("/")[-1]

                # when
                resp2 = await client.patch(
                    f"/api/v1/uploads/{session_id}",
                    data=b"y" * 10,
                    headers={"Upload-Offset": "0"},
                )
                # then
                self.assertEqual(resp2.status, 201)
                record = node_record_from_dict(await resp2.json())

        self.assertEqual(record.id, "syno-99")
        self.assertEqual(record.name, "photo.jpg")

    async def test_partial_patch_returns_204(self) -> None:
        """PATCH with partial data returns 204 with updated Upload-Offset."""
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)

        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/parent-a/uploads",
                json={"name": "big.bin", "size": 20},
            )
            # then
            self.assertEqual(resp.status, 201)
            session_id = resp.headers["Location"].split("/")[-1]

            # when
            resp2 = await client.patch(
                f"/api/v1/uploads/{session_id}",
                data=b"a" * 10,
                headers={"Upload-Offset": "0"},
            )
            # then
            self.assertEqual(resp2.status, 204)
            self.assertEqual(resp2.headers["Upload-Offset"], "10")

    async def test_session_not_found_returns_404(self) -> None:
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror, self._spool)

        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/uploads/no-such-session",
                data=b"x",
                headers={"Upload-Offset": "0"},
            )
            # then
            self.assertEqual(resp.status, 404)

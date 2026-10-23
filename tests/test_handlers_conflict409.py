"""Name-conflict handlers return 409 with a node record body (same shape as 201)."""

import asyncio
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.client.remote._lib import node_record_from_dict
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.handlers.nodes import create_node, upload_node
from wcpan.drive.server.handlers.upload import (
    create_upload_session,
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
from wcpan.drive.server.services.paths import SynologyPathService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadService,
    UploadSessionStore,
)
from wcpan.drive.server.types import NodeObservation, SynologyLocator
from wcpan.drive.server.workers import create_write_queue
from wcpan.synology import SynologyUploadConflictError


# Patch targets for find_child_by_name — now a method on SynologyPathService
_FIND_CHILD_CLS = SynologyPathService


_EXISTING_DIR = {
    "file_id": "dir-existing",
    "permanent_link": "dir-existing",
    "parent_id": "",
    "name": "newfolder",
    "type": "dir",
    "content_type": "dir",
    "size": 0,
    "created_time": 1_700_000_000,
    "modified_time": 1_700_000_000,
    "change_time": 0,
    "sync_id": 0,
}

_EXISTING_FILE = {
    "file_id": "file-existing",
    "permanent_link": "file-existing",
    "parent_id": "",
    "name": "dup.bin",
    "type": "file",
    "content_type": "file",
    "size": 3,
    "created_time": 1_700_000_000,
    "modified_time": 1_700_000_000,
    "change_time": 0,
    "sync_id": 0,
}

_ENRICH = "wcpan.drive.server.services.enricher.MediaEnrichService.enrich"


class _FakeOffMain:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeStorage:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._locators: dict[str, SynologyLocator] = {}

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> int:
        for o in observations:
            self._nodes[o.record.id] = o.record
            if o.locator is not None:
                self._locators[o.record.id] = o.locator
        return len(observations)

    async def get_locators(self, node_ids: list[str]) -> dict[str, SynologyLocator]:
        # Fixture nodes use their own ID as the Synology file_id unless rebound.
        return {
            i: self._locators.get(i, SynologyLocator(i))
            for i in node_ids
            if i in self._nodes
        }


def _make_dir(node_id: str) -> NodeRecord:
    t = 0
    return NodeRecord(
        id=node_id,
        parent_id=None,
        name=node_id,
        is_directory=True,
        created_time=t,
        modified_time=t,
        changed_time=t,
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


def _make_app(
    storage: _FakeStorage, session_store: UploadSessionStore
) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    storage._nodes.setdefault("parent-a", _make_dir("parent-a"))
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
    app[READY_KEY] = True
    app[STORAGE_KEY] = storage
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq
    app[SYNOLOGY_PATH_KEY] = syno_paths
    app[CHANGE_SERVICE_KEY] = node_sync
    app[SYNOLOGY_DRIVE_API_KEY] = MagicMock()
    app[UPLOAD_SERVICE_KEY] = UploadService(
        store=session_store,
        node_sync=node_sync,
        drive_api=app[SYNOLOGY_DRIVE_API_KEY],
        syno_paths=syno_paths,
    )

    app.router.add_post("/api/v1/nodes", create_node)
    app.router.add_post("/api/v1/nodes/{parent_id}/upload", upload_node)
    app.router.add_post(
        "/api/v1/nodes/{parent_id}/upload-session", create_upload_session
    )
    app.router.add_patch("/api/v1/upload-sessions/{session_id}", patch_upload_chunk)
    return app


async def _passthrough_enrich(record: NodeRecord, *args, **kwargs) -> NodeRecord:
    return record


class TestCreateNodeConflict409(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_returns_node_record_json(self) -> None:
        storage = _FakeStorage()
        app = _make_app(storage, self._store)

        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY], "create_folder", new_callable=AsyncMock
            ) as mock_cf,
            patch.object(
                _FIND_CHILD_CLS,
                "find_child_by_name",
                new_callable=AsyncMock,
                return_value=_EXISTING_DIR,
            ),
            patch(_ENRICH, new_callable=AsyncMock, side_effect=_passthrough_enrich),
        ):
            mock_cf.side_effect = SynologyUploadConflictError("exists", file_name="x")
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes",
                    json={"name": "newfolder", "parent_id": "parent-a"},
                )
                self.assertEqual(resp.status, 409)
                record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, "dir-existing")
        self.assertEqual(record.name, "newfolder")
        self.assertTrue(record.is_directory)
        self.assertEqual(record.parent_id, "parent-a")


class TestFinaliseUploadConflict409(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_put_session_finalise_conflict_returns_409_node(self) -> None:
        storage = _FakeStorage()
        app = _make_app(storage, self._store)
        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY], "upload", new_callable=AsyncMock
            ) as mock_up,
            patch.object(
                _FIND_CHILD_CLS,
                "find_child_by_name",
                new_callable=AsyncMock,
                side_effect=[None, _EXISTING_FILE],
            ),
            patch(_ENRICH, new_callable=AsyncMock, side_effect=_passthrough_enrich),
        ):
            mock_up.side_effect = SynologyUploadConflictError(
                "exists", file_name="dup.bin"
            )
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/parent-a/upload-session",
                    json={"name": "dup.bin", "size": 10},
                )
                self.assertEqual(resp.status, 201)
                sid = resp.headers["Location"].split("/")[-1]
                resp2 = await client.patch(
                    f"/api/v1/upload-sessions/{sid}",
                    data=b"x" * 10,
                    headers={"Upload-Offset": "0"},
                )
                self.assertEqual(resp2.status, 409)
                record = node_record_from_dict(await resp2.json())
        self.assertEqual(record.id, "file-existing")
        self.assertEqual(record.name, "dup.bin")


class TestUploadNodeConflict409(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_returns_node_record_json(self) -> None:
        storage = _FakeStorage()
        app = _make_app(storage, self._store)

        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY], "upload", new_callable=AsyncMock
            ) as mock_up,
            patch.object(
                _FIND_CHILD_CLS,
                "find_child_by_name",
                new_callable=AsyncMock,
                return_value=_EXISTING_FILE,
            ),
            patch(_ENRICH, new_callable=AsyncMock, side_effect=_passthrough_enrich),
        ):
            mock_up.side_effect = SynologyUploadConflictError("exists", file_name="x")
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/parent-a/upload",
                    data=b"abc",
                    params={"name": "dup.bin"},
                )
                self.assertEqual(resp.status, 409)
                record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, "file-existing")
        self.assertEqual(record.name, "dup.bin")
        self.assertFalse(record.is_directory)

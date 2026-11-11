"""Name-conflict handlers return 409 with a node record body (same shape as 201)."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from decoy import matchers
from wcpan.drive.protocol.lib import node_record_from_dict
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.handlers.nodes import create_node, upload_node
from wcpan.drive.server.handlers.upload import (
    create_upload_session,
    patch_upload_chunk,
)
from wcpan.drive.server.keys import (
    CHANGE_SERVICE_KEY,
    MIRROR_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
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
from wcpan.drive.server.synology._operations import SynologyNodeOperations
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._upload import SynologyUploader
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import create_write_queue
from wcpan.synology import (
    SynologyChildRef,
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyUploadConflictError,
)

from ._lib import DecoyTestCase


_PARENT = SynologyFileId(file_id="parent-a")

_EXISTING_DIR: SynologyFileInfo = {
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

_EXISTING_FILE: SynologyFileInfo = {
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
    mirror: _FakeMirror,
    spool: SpoolArea,
    *,
    drive_api: SynologyClient,
) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    mirror._nodes.setdefault("parent-a", _make_dir("parent-a"))
    syno_paths = SynologyPathService(
        registry=MountRegistry(mounts={}, root_ids={}),
        store=mirror,  # type: ignore[arg-type]
    )
    node_sync = NodeSyncService(
        mirror=mirror,
        write_queue=wq,
        off_main=off_main,
        mounts={},
        metadata_queue=asyncio.Queue(),
    )  # type: ignore[arg-type]
    app[READY_KEY] = True
    app[MIRROR_KEY] = mirror
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq
    app[CHANGE_SERVICE_KEY] = node_sync
    app[NODE_OPERATIONS_KEY] = SynologyNodeOperations(
        drive_api=drive_api,
        syno_paths=syno_paths,
        node_sync=node_sync,
    )
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

    app.router.add_post("/api/v1/nodes", create_node)
    app.router.add_post("/api/v1/nodes/{parent_id}/upload", upload_node)
    app.router.add_post(
        "/api/v1/nodes/{parent_id}/upload-session", create_upload_session
    )
    app.router.add_patch("/api/v1/upload-sessions/{session_id}", patch_upload_chunk)
    return app


async def _passthrough_enrich(record: NodeRecord, *args, **kwargs) -> NodeRecord:
    return record


class _ConflictTestCase(DecoyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        spool = self.enterContext(create_spool_area(tmp_dir=Path(self._tmp.name)))
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.app = _make_app(_FakeMirror(), spool, drive_api=self.drive_api)
        enrich = patch(_ENRICH, new_callable=AsyncMock, side_effect=_passthrough_enrich)
        enrich.start()
        self.addCleanup(enrich.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _rehearse_get_child(self, name: str):
        return await self.drive_api.get_file(
            SynologyChildRef(parent_ref=_PARENT, name=name)
        )

    async def _rehearse_upload(self):
        return await self.drive_api.upload(
            parent_path=_PARENT,
            name="dup.bin",
            data=matchers.Anything(),
            mime_type=matchers.AnythingOrNone(),
        )


class TestCreateNodeConflict409(_ConflictTestCase):
    async def test_returns_node_record_json(self) -> None:
        # given
        self.decoy.when(
            await self.drive_api.create_folder(_PARENT, "newfolder")
        ).then_raise(SynologyUploadConflictError("exists", file_name="x"))
        self.decoy.when(await self._rehearse_get_child("newfolder")).then_return(
            _EXISTING_DIR
        )

        async with TestClient(TestServer(self.app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes",
                json={"name": "newfolder", "parent_id": "parent-a"},
            )
            # then
            self.assertEqual(resp.status, 409)
            record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, "dir-existing")
        self.assertEqual(record.name, "newfolder")
        self.assertTrue(record.is_directory)
        self.assertEqual(record.parent_id, "parent-a")


class TestFinaliseUploadConflict409(_ConflictTestCase):
    async def test_put_session_finalise_conflict_returns_409_node(self) -> None:
        # given
        self.decoy.when(await self._rehearse_upload()).then_raise(
            SynologyUploadConflictError("exists", file_name="dup.bin")
        )
        # absent when the session opens, present once the upload conflicts
        self.decoy.when(await self._rehearse_get_child("dup.bin")).then_return(
            None, _EXISTING_FILE
        )

        async with TestClient(TestServer(self.app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/parent-a/upload-session",
                json={"name": "dup.bin", "size": 10},
            )
            # then
            self.assertEqual(resp.status, 201)
            sid = resp.headers["Location"].split("/")[-1]
            # when
            resp2 = await client.patch(
                f"/api/v1/upload-sessions/{sid}",
                data=b"x" * 10,
                headers={"Upload-Offset": "0"},
            )
            # then
            self.assertEqual(resp2.status, 409)
            record = node_record_from_dict(await resp2.json())
        self.assertEqual(record.id, "file-existing")
        self.assertEqual(record.name, "dup.bin")


class TestUploadNodeConflict409(_ConflictTestCase):
    async def test_returns_node_record_json(self) -> None:
        # given
        self.decoy.when(await self._rehearse_upload()).then_raise(
            SynologyUploadConflictError("exists", file_name="x")
        )
        self.decoy.when(await self._rehearse_get_child("dup.bin")).then_return(
            _EXISTING_FILE
        )

        async with TestClient(TestServer(self.app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/parent-a/upload",
                data=b"abc",
                params={"name": "dup.bin"},
            )
            # then
            self.assertEqual(resp.status, 409)
            record = node_record_from_dict(await resp.json())

        self.assertEqual(record.id, "file-existing")
        self.assertEqual(record.name, "dup.bin")
        self.assertFalse(record.is_directory)

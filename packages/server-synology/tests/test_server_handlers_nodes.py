"""Tests for node CRUD, download, and single-shot upload handlers."""

import asyncio
import tempfile
import unicodedata
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from decoy import matchers
from wcpan.drive.protocol.lib import node_record_from_dict
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.handlers.nodes import (
    create_node,
    delete_node,
    download_node,
    get_node,
    update_node,
    upload_node,
)
from wcpan.drive.server.keys import (
    MIRROR_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.lib.root import SERVER_ROOT_ID
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
    SynologyPermanentLink,
    SynologyUploadConflictError,
    SynologyUploadError,
)

from ._lib import DecoyTestCase, IdentityIds


_EPOCH = 0
_P1 = SynologyFileId(file_id="p1")
_N1 = SynologyPermanentLink(permanent_link="n1")

_FAKE_SYNO_INFO: SynologyFileInfo = {
    "file_id": "new-dir",
    "permanent_link": "new-dir",
    "parent_id": "p1",
    "name": "docs",
    "type": "dir",
    "content_type": "dir",
    "size": 0,
    "created_time": 1_000_000,
    "modified_time": 1_000_000,
    "change_time": 0,
    "sync_id": 1,
}


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

    async def delete_subtree_and_emit_changes(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)


def _make_node(
    node_id: str = "n1",
    parent_id: str = "p1",
    name: str = "test.txt",
    *,
    is_directory: bool = False,
    mime_type: str = "text/plain",
    size: int = 100,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type=mime_type,
        hash="abc",
        hash_algorithm="md4",
        size=size,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _make_dir(
    node_id: str,
    parent_id: str | None = None,
    name: str | None = None,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name or node_id,
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


def _rename_result(name: str) -> SynologyFileInfo:
    return cast(
        SynologyFileInfo,
        {
            "name": name,
            "created_time": 1000,
            "modified_time": 2000,
            "change_time": 0,
            "hash": "abc",
            "size": 100,
        },
    )


def _upload_info(name: str) -> SynologyFileInfo:
    return {
        "file_id": "uploaded-1",
        "permanent_link": "uploaded-1",
        "parent_id": "p1",
        "name": name,
        "type": "file",
        "content_type": "file",
        "size": 5,
        "created_time": 1000,
        "modified_time": 1000,
        "change_time": 0,
        "sync_id": 1,
    }


class _NodesTestCase(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.mirror = _FakeMirror()
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.decoy.when(
            await self.drive_api.list_folder(
                matchers.Anything(), offset=matchers.Anything()
            )
        ).then_return(([], 0))

    def _make_app(self) -> web.Application:
        mirror = self.mirror
        app = web.Application()
        off_main = _FakeOffMain()
        wq = create_write_queue()
        tmp = tempfile.mkdtemp(prefix="wcpan_test_")
        spool = self.enterContext(create_spool_area(tmp_dir=Path(tmp)))
        mirror._nodes.setdefault("p1", _make_dir("p1"))
        mirror._nodes.setdefault("new-parent", _make_dir("new-parent"))
        mirror._nodes.setdefault("bad", _make_dir("bad"))
        syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=Mounts(), root_ids={}),
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
        self.node_sync = node_sync
        app[NODE_OPERATIONS_KEY] = SynologyNodeOperations(
            drive_api=self.drive_api,
            syno_paths=syno_paths,
            node_sync=node_sync,
        )
        uploader = SynologyUploader(
            drive_api=self.drive_api,
            syno_paths=syno_paths,
            node_sync=node_sync,
        )
        app[UPLOAD_SERVICE_KEY] = UploadService(
            store=UploadSessionStore(),
            uploader=uploader,
            files=SpooledFiles(uploader=uploader, area=spool),
        )

        app.router.add_get("/api/v1/nodes/{id}", get_node)
        app.router.add_get("/api/v1/nodes/{id}/download", download_node)
        app.router.add_post("/api/v1/nodes", create_node)
        app.router.add_patch("/api/v1/nodes/{id}", update_node)
        app.router.add_delete("/api/v1/nodes/{id}", delete_node)
        app.router.add_post("/api/v1/nodes/{parent_id}/upload", upload_node)
        return app

    async def _rehearse_upload(self, name: object = matchers.Anything()):
        return await self.drive_api.upload(
            parent_path=_P1,
            name=name,  # type: ignore[arg-type]
            data=matchers.Anything(),
            mime_type=matchers.AnythingOrNone(),
        )


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------


class TestGetNode(_NodesTestCase):
    async def test_returns_node(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/nodes/n1")
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "n1")
        self.assertEqual(record.name, "test.txt")

    async def test_not_found(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/nodes/missing")
            # then
            self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# create_node (success path)
# ---------------------------------------------------------------------------


class TestCreateNode(_NodesTestCase):
    async def test_success(self):
        # given
        app = self._make_app()
        self.decoy.when(await self.drive_api.create_folder(_P1, "docs")).then_return(
            _FAKE_SYNO_INFO
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes",
                json={"name": "docs", "parent_id": "p1"},
            )
            # then
            self.assertEqual(resp.status, 201)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "new-dir")
        self.assertTrue(record.is_directory)

    async def test_name_is_normalized_to_nfc(self):
        # given
        raw_name = "てすとば"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        app = self._make_app()
        self.decoy.when(
            await self.drive_api.create_folder(_P1, normalized_name)
        ).then_return({**_FAKE_SYNO_INFO, "name": normalized_name})
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes",
                json={"name": raw_name, "parent_id": "p1"},
            )
            # then
            self.assertEqual(resp.status, 201)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.name, normalized_name)

    async def test_missing_name(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes",
                json={"parent_id": "p1"},
            )
            # then
            self.assertEqual(resp.status, 400)

    async def test_missing_parent_id(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes",
                json={"name": "docs"},
            )
            # then
            self.assertEqual(resp.status, 400)


# ---------------------------------------------------------------------------
# update_node
# ---------------------------------------------------------------------------


class TestUpdateNode(_NodesTestCase):
    async def test_rename(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        self.decoy.when(await self.drive_api.rename(_N1, "renamed.txt")).then_return(
            _rename_result("renamed.txt")
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"name": "renamed.txt"},
            )
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.name, "renamed.txt")

    async def test_rename_name_is_normalized_to_nfc(self):
        # given
        raw_name = "かなぴ.txt"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        self.decoy.when(await self.drive_api.rename(_N1, normalized_name)).then_return(
            _rename_result(normalized_name)
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"name": raw_name},
            )
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.name, normalized_name)

    async def test_move(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"parent_id": "new-parent"},
            )
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.parent_id, "new-parent")
        self.assertEqual(record.id, "n1")
        self.decoy.verify(
            await self.drive_api.move(_N1, SynologyFileId(file_id="new-parent")),
            times=1,
        )

    async def test_move_rebinds_locator_via_permanent_link(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        node_sync = self.node_sync
        self.decoy.when(await self.drive_api.get_file(_N1)).then_return(
            {
                "file_id": "n2",
                "parent_id": "new-parent",
                "permanent_link": "n1",
                "name": "test.txt",
                "type": "file",
                "content_type": "document",
                "size": 100,
                "created_time": 1000,
                "modified_time": 2000,
                "change_time": 0,
                "sync_id": 1,
                "hash": "def",
            }
        )
        with patch.object(node_sync, "delete", new_callable=AsyncMock) as delete_mock:
            async with TestClient(TestServer(app)) as client:
                # when
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"parent_id": "new-parent"},
                )
                # then
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                record = node_record_from_dict(body)
        delete_mock.assert_not_awaited()
        self.assertEqual(record.id, "n1")
        self.assertEqual(record.parent_id, "new-parent")
        # The locator stays private.
        self.assertNotIn("mutable_id", body)
        # when
        wq = app[WRITE_QUEUE_KEY]
        while not wq.empty():
            await wq.get_nowait()()
        # then
        self.assertEqual(await self.mirror.get_locators(["n1"]), {"n1": "n2"})

    async def test_rename_conflict(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        self.decoy.when(await self.drive_api.rename(_N1, "conflict.txt")).then_raise(
            SynologyUploadConflictError("exists", file_name="x")
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"name": "conflict.txt"},
            )
            # then
            self.assertEqual(resp.status, 409)

    async def test_virtual_node_forbidden(self):
        # given
        self.mirror._nodes["root"] = _make_dir("root")
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/root",
                json={"name": "new"},
            )
            # then
            self.assertEqual(resp.status, 403)

    async def test_not_found(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/missing",
                json={"name": "new"},
            )
            # then
            self.assertEqual(resp.status, 404)

    async def test_move_error(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        self.decoy.when(
            await self.drive_api.move(_N1, SynologyFileId(file_id="bad"))
        ).then_raise(Exception("move failed"))
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"parent_id": "bad"},
            )
        # then
        self.assertEqual(resp.status, 503)


# ---------------------------------------------------------------------------
# delete_node
# ---------------------------------------------------------------------------


class TestDeleteNode(_NodesTestCase):
    async def test_success(self):
        # given
        self.mirror._nodes["n1"] = _make_node()
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.delete("/api/v1/nodes/n1")
            # then
            self.assertEqual(resp.status, 204)
        self.decoy.verify(await self.drive_api.delete(_N1), times=1)

    async def test_not_found(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.delete("/api/v1/nodes/missing")
            # then
            self.assertEqual(resp.status, 404)

    async def test_underscore_id_is_an_ordinary_node(self):
        # given
        self.mirror._nodes["_n1"] = _make_node(node_id="_n1")
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.delete("/api/v1/nodes/_n1")
            # then
            self.assertEqual(resp.status, 204)
        self.decoy.verify(
            await self.drive_api.delete(SynologyPermanentLink(permanent_link="_n1")),
            times=1,
        )

    async def test_virtual_node_forbidden(self):
        # given
        self.mirror._nodes["mount"] = _make_dir("mount", SERVER_ROOT_ID)
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.delete("/api/v1/nodes/mount")
            # then
            self.assertEqual(resp.status, 403)


# ---------------------------------------------------------------------------
# upload_node (success path)
# ---------------------------------------------------------------------------


class TestUploadNode(_NodesTestCase):
    async def test_success(self):
        # given
        app = self._make_app()
        self.decoy.when(await self._rehearse_upload("data.bin")).then_return(
            _upload_info("data.bin")
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
                params={"name": "data.bin"},
            )
            # then
            self.assertEqual(resp.status, 201)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "uploaded-1")
        self.assertEqual(record.name, "data.bin")

    async def test_upload_name_is_normalized_to_nfc(self):
        # given
        raw_name = "かなば.txt"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        app = self._make_app()
        self.decoy.when(await self._rehearse_upload(normalized_name)).then_return(
            _upload_info(normalized_name)
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
                params={"name": raw_name},
            )
            # then
            self.assertEqual(resp.status, 201)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.name, normalized_name)

    async def test_missing_name(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
            )
            # then
            self.assertEqual(resp.status, 400)

    async def test_upload_to_virtual_root_forbidden(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/_/upload",
                data=b"hello",
                params={"name": "x.bin"},
            )
            # then
            self.assertEqual(resp.status, 403)

    async def test_upload_error(self):
        # given
        app = self._make_app()
        self.decoy.when(await self._rehearse_upload()).then_raise(
            SynologyUploadError("fail", file_name="x")
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
                params={"name": "x.bin"},
            )
            # then
            self.assertEqual(resp.status, 507)

    async def test_conflict_returns_409_without_hitting_synology_upload(self):
        # given
        app = self._make_app()
        self.decoy.when(
            await self.drive_api.get_file(
                SynologyChildRef(parent_ref=_P1, name="data.bin")
            )
        ).then_return(
            {
                "file_id": "existing-1",
                "permanent_link": "existing-1",
                "parent_id": "p1",
                "display_path": "/volume1/p1/data.bin",
                "name": "data.bin",
                "type": "file",
                "content_type": "file",
                "size": 5,
                "created_time": 1000,
                "modified_time": 1000,
                "change_time": 0,
                "sync_id": 1,
            }
        )
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
                params={"name": "data.bin"},
            )
            # then
            self.assertEqual(resp.status, 409)
        self.decoy.verify(await self._rehearse_upload(), times=0)


# ---------------------------------------------------------------------------
# download_node
# ---------------------------------------------------------------------------


class TestDownloadNode(_NodesTestCase):
    async def test_not_found(self):
        # given
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/nodes/missing/download")
            # then
            self.assertEqual(resp.status, 404)

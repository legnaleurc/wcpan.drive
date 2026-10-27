"""Tests for node CRUD, download, and single-shot upload handlers."""

import asyncio
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.protocol.lib import node_record_from_dict
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.handlers.nodes import (
    create_node,
    delete_node,
    download_node,
    get_node,
    update_node,
    upload_node,
)
from wcpan.drive.server.keys import (
    CHANGE_SERVICE_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    STORAGE_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadService,
    UploadSessionStore,
)
from wcpan.drive.server.synology.convert import SynologyLocator
from wcpan.drive.server.synology.mounts import MountRegistry
from wcpan.drive.server.synology.operations import SynologyNodeOperations
from wcpan.drive.server.synology.paths import SynologyPathService
from wcpan.drive.server.synology.upload import SynologyUploader
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import create_write_queue
from wcpan.synology import (
    SynologyUploadConflictError,
    SynologyUploadError,
)


# Test-local slots for the Synology fakes; the backend no longer uses app keys.
SYNOLOGY_DRIVE_API_KEY: web.AppKey[Any] = web.AppKey("synology_drive_api")
SYNOLOGY_PATH_KEY: web.AppKey[Any] = web.AppKey("synology_path")


_EPOCH = 0

_FAKE_SYNO_INFO = {
    "file_id": "new-dir",
    "permanent_link": "new-dir",
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


class _FakeStorage:
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


def _make_app(storage: _FakeStorage) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    tmp = tempfile.mkdtemp(prefix="wcpan_test_")
    store = UploadSessionStore(tmp_dir=Path(tmp))
    storage._nodes.setdefault("p1", _make_dir("p1"))
    storage._nodes.setdefault("new-parent", _make_dir("new-parent"))
    storage._nodes.setdefault("bad", _make_dir("bad"))
    syno_paths = SynologyPathService(
        registry=MountRegistry(mounts={}, root_ids={}),
        store=storage,  # type: ignore[arg-type]
    )
    node_sync = NodeSyncService(
        storage=storage,
        write_queue=wq,
        off_main=off_main,
        mounts={},
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
    app[NODE_OPERATIONS_KEY] = SynologyNodeOperations(
        drive_api=drive_api,
        syno_paths=syno_paths,
        node_sync=node_sync,
    )
    app[UPLOAD_SERVICE_KEY] = UploadService(
        store=store,
        uploader=SynologyUploader(
            drive_api=drive_api,
            syno_paths=syno_paths,
            node_sync=node_sync,
        ),
    )

    app.router.add_get("/api/v1/nodes/{id}", get_node)
    app.router.add_get("/api/v1/nodes/{id}/download", download_node)
    app.router.add_post("/api/v1/nodes", create_node)
    app.router.add_patch("/api/v1/nodes/{id}", update_node)
    app.router.add_delete("/api/v1/nodes/{id}", delete_node)
    app.router.add_post("/api/v1/nodes/{parent_id}/upload", upload_node)
    return app


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------


class TestGetNode(IsolatedAsyncioTestCase):
    async def test_returns_node(self):
        storage = _FakeStorage()
        node = _make_node()
        storage._nodes["n1"] = node
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/nodes/n1")
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "n1")
        self.assertEqual(record.name, "test.txt")

    async def test_not_found(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/nodes/missing")
            self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# create_node (success path)
# ---------------------------------------------------------------------------


class TestCreateNode(IsolatedAsyncioTestCase):
    async def test_success(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "create_folder",
            new_callable=AsyncMock,
            return_value=_FAKE_SYNO_INFO,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes",
                    json={"name": "docs", "parent_id": "p1"},
                )
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "new-dir")
        self.assertTrue(record.is_directory)

    async def test_name_is_normalized_to_nfc(self):
        raw_name = "てすとは\u3099"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        storage = _FakeStorage()
        app = _make_app(storage)
        result = dict(_FAKE_SYNO_INFO, name=normalized_name)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "create_folder",
            new_callable=AsyncMock,
            return_value=result,
        ) as create_folder:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes",
                    json={"name": raw_name, "parent_id": "p1"},
                )
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(create_folder.await_args.args[1], normalized_name)
        self.assertEqual(record.name, normalized_name)

    async def test_missing_name(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/nodes",
                json={"parent_id": "p1"},
            )
            self.assertEqual(resp.status, 400)

    async def test_missing_parent_id(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/nodes",
                json={"name": "docs"},
            )
            self.assertEqual(resp.status, 400)


# ---------------------------------------------------------------------------
# update_node
# ---------------------------------------------------------------------------


class TestUpdateNode(IsolatedAsyncioTestCase):
    async def test_rename(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        rename_result = {
            "name": "renamed.txt",
            "created_time": 1000,
            "modified_time": 2000,
            "change_time": 0,
            "hash": "abc",
            "size": 100,
        }
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "rename",
            new_callable=AsyncMock,
            return_value=rename_result,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"name": "renamed.txt"},
                )
                self.assertEqual(resp.status, 200)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(record.name, "renamed.txt")

    async def test_rename_name_is_normalized_to_nfc(self):
        raw_name = "かなひ\u309a.txt"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        rename_result = {
            "name": normalized_name,
            "created_time": 1000,
            "modified_time": 2000,
            "change_time": 0,
            "hash": "abc",
            "size": 100,
        }
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "rename",
            new_callable=AsyncMock,
            return_value=rename_result,
        ) as rename_node:
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"name": raw_name},
                )
                self.assertEqual(resp.status, 200)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(rename_node.await_args.args[1], normalized_name)
        self.assertEqual(record.name, normalized_name)

    async def test_move(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        app[SYNOLOGY_DRIVE_API_KEY].move = AsyncMock()
        app[SYNOLOGY_DRIVE_API_KEY].get_file = AsyncMock(return_value=None)
        app[SYNOLOGY_DRIVE_API_KEY].list_folder = AsyncMock(return_value=([], 0))
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/v1/nodes/n1",
                json={"parent_id": "new-parent"},
            )
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.parent_id, "new-parent")
        self.assertEqual(record.id, "n1")

    async def test_move_rebinds_locator_via_permanent_link(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        node_sync = app[CHANGE_SERVICE_KEY]
        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY],
                "move",
                new_callable=AsyncMock,
            ),
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY],
                "get_file",
                new_callable=AsyncMock,
                return_value={
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
                },
            ),
            patch.object(node_sync, "delete", new_callable=AsyncMock) as delete_mock,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"parent_id": "new-parent"},
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                record = node_record_from_dict(body)
        delete_mock.assert_not_awaited()
        self.assertEqual(record.id, "n1")
        self.assertEqual(record.parent_id, "new-parent")
        # The locator stays private.
        self.assertNotIn("mutable_id", body)
        wq = app[WRITE_QUEUE_KEY]
        while not wq.empty():
            await wq.get_nowait()()
        self.assertEqual(await storage.get_locators(["n1"]), {"n1": "n2"})

    async def test_rename_conflict(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "rename",
            new_callable=AsyncMock,
            side_effect=SynologyUploadConflictError("exists", file_name="x"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"name": "conflict.txt"},
                )
                self.assertEqual(resp.status, 409)

    async def test_virtual_node_forbidden(self):
        storage = _FakeStorage()
        storage._nodes["_root"] = _make_node(node_id="_root")
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/v1/nodes/_root",
                json={"name": "new"},
            )
            self.assertEqual(resp.status, 403)

    async def test_not_found(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/v1/nodes/missing",
                json={"name": "new"},
            )
            self.assertEqual(resp.status, 404)

    async def test_move_error(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "move",
            new_callable=AsyncMock,
            side_effect=Exception("move failed"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/v1/nodes/n1",
                    json={"parent_id": "bad"},
                )
        self.assertEqual(resp.status, 503)


# ---------------------------------------------------------------------------
# delete_node
# ---------------------------------------------------------------------------


class TestDeleteNode(IsolatedAsyncioTestCase):
    async def test_success(self):
        storage = _FakeStorage()
        storage._nodes["n1"] = _make_node()
        app = _make_app(storage)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "delete",
            new_callable=AsyncMock,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/v1/nodes/n1")
                self.assertEqual(resp.status, 204)

    async def test_not_found(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/v1/nodes/missing")
            self.assertEqual(resp.status, 404)

    async def test_virtual_node_forbidden(self):
        storage = _FakeStorage()
        storage._nodes["_mount"] = _make_node(node_id="_mount")
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/v1/nodes/_mount")
            self.assertEqual(resp.status, 403)


# ---------------------------------------------------------------------------
# upload_node (success path)
# ---------------------------------------------------------------------------


class TestUploadNode(IsolatedAsyncioTestCase):
    async def test_success(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        upload_info = {
            "file_id": "uploaded-1",
            "permanent_link": "uploaded-1",
            "name": "data.bin",
            "type": "file",
            "content_type": "file",
            "size": 5,
            "created_time": 1000,
            "modified_time": 1000,
            "change_time": 0,
            "sync_id": 1,
        }
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=upload_info,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/p1/upload",
                    data=b"hello",
                    params={"name": "data.bin"},
                )
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, "uploaded-1")
        self.assertEqual(record.name, "data.bin")

    async def test_upload_name_is_normalized_to_nfc(self):
        raw_name = "かなは\u3099.txt"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        storage = _FakeStorage()
        app = _make_app(storage)
        upload_info = {
            "file_id": "uploaded-1",
            "permanent_link": "uploaded-1",
            "name": normalized_name,
            "type": "file",
            "content_type": "file",
            "size": 5,
            "created_time": 1000,
            "modified_time": 1000,
            "change_time": 0,
            "sync_id": 1,
        }
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            return_value=upload_info,
        ) as upload_file:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/p1/upload",
                    data=b"hello",
                    params={"name": raw_name},
                )
                self.assertEqual(resp.status, 201)
                record = node_record_from_dict(await resp.json())
        self.assertEqual(upload_file.await_args.kwargs["name"], normalized_name)
        self.assertEqual(record.name, normalized_name)

    async def test_missing_name(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
            )
            self.assertEqual(resp.status, 400)

    async def test_upload_to_virtual_root_forbidden(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/nodes/_/upload",
                data=b"hello",
                params={"name": "x.bin"},
            )
            self.assertEqual(resp.status, 403)

    async def test_upload_error(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        with patch.object(
            app[SYNOLOGY_DRIVE_API_KEY],
            "upload",
            new_callable=AsyncMock,
            side_effect=SynologyUploadError("fail", file_name="x"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/v1/nodes/p1/upload",
                    data=b"hello",
                    params={"name": "x.bin"},
                )
                self.assertEqual(resp.status, 507)

    async def test_conflict_returns_409_without_hitting_synology_upload(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        app[SYNOLOGY_DRIVE_API_KEY].get_file = AsyncMock(
            return_value={
                "file_id": "p1",
                "permanent_link": "p1",
                "parent_id": "root",
                "display_path": "/volume1/p1",
                "name": "p1",
                "type": "dir",
                "content_type": "dir",
                "size": 0,
                "created_time": 1000,
                "modified_time": 1000,
                "change_time": 0,
                "sync_id": 0,
            }
        )
        app[SYNOLOGY_DRIVE_API_KEY].get_file = AsyncMock(
            return_value={
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
            resp = await client.post(
                "/api/v1/nodes/p1/upload",
                data=b"hello",
                params={"name": "data.bin"},
            )
            self.assertEqual(resp.status, 409)
        app[SYNOLOGY_DRIVE_API_KEY].upload.assert_not_called()


# ---------------------------------------------------------------------------
# download_node
# ---------------------------------------------------------------------------


class TestDownloadNode(IsolatedAsyncioTestCase):
    async def test_not_found(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/nodes/missing/download")
            self.assertEqual(resp.status, 404)

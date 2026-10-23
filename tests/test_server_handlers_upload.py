"""Tests for the four resumable upload session handlers."""

import asyncio
import json
import tempfile
import unicodedata
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiohttp import web
from wcpan.drive.protocol.types import MirrorMutableId
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
from wcpan.drive.server.services.paths import SynologyPathService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    _L,
    UploadService,
    UploadSessionStore,
)
from wcpan.synology import (
    SynologyNameTooLongError,
    SynologyNetworkError,
    SynologyUploadError,
)


class _MockOffMain:
    """Executes functions synchronously for testing."""

    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _make_app(store: UploadSessionStore) -> dict:
    app: dict = {}
    off_main = _MockOffMain()
    wq: asyncio.Queue = asyncio.Queue()
    storage = MagicMock()

    async def _get_node_by_id(node_id: str):
        if node_id == "p1":
            return MagicMock(mutable_id=MirrorMutableId("p1"))
        return None

    storage.get_node_by_id = AsyncMock(side_effect=_get_node_by_id)
    syno_paths = SynologyPathService(
        registry=MountRegistry(mounts={"tmp": "/team-folders/download"}, root_ids={}),
        storage=storage,
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
    app[OFF_MAIN_KEY] = off_main
    app[STORAGE_KEY] = storage
    app[WRITE_QUEUE_KEY] = wq
    app[SYNOLOGY_DRIVE_API_KEY] = drive_api
    app[SYNOLOGY_PATH_KEY] = syno_paths
    app[CHANGE_SERVICE_KEY] = node_sync
    app[UPLOAD_SERVICE_KEY] = UploadService(
        store=store,
        node_sync=node_sync,
        drive_api=drive_api,
        syno_paths=syno_paths,
    )
    return app


def _async_chunks(data: bytes, chunk_size: int = 65536):
    """Return an async iterable that yields data in chunk_size pieces."""

    async def _gen():
        for i in range(0, max(len(data), 1), chunk_size):
            yield data[i : i + chunk_size]

    return _gen()


def _make_request(
    *,
    app: dict | None = None,
    match_info: dict | None = None,
    headers: dict | None = None,
    body: bytes = b"",
    json_body: dict | None = None,
) -> MagicMock:
    req = MagicMock(spec=web.Request)
    req.app = app if app is not None else {}
    req.match_info = match_info or {}
    req.headers = headers or {}
    req.read = AsyncMock(return_value=body)
    req.json = AsyncMock(return_value=json_body or {})
    # Mock streaming content for iter_chunked
    content = MagicMock()
    content.iter_chunked = lambda _size: _async_chunks(body)
    req.content = content
    return req


# ---------- create_upload_session ----------


class TestCreateUploadSession(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_success_returns_201_with_location_and_upload_length(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"name": "file.bin", "size": 1024},
        )
        resp = await create_upload_session(req)
        self.assertEqual(resp.status, 201)
        self.assertIn("/api/v1/uploads/", resp.headers["Location"])
        self.assertEqual(resp.headers["Upload-Length"], "1024")

    async def test_name_is_normalized_to_nfc(self):
        raw_name = "かなは\u3099.txt"
        normalized_name = unicodedata.normalize("NFC", raw_name)
        app = _make_app(self._store)
        req = _make_request(
            app=app,
            match_info={"parent_id": "p1"},
            json_body={"name": raw_name, "size": 1024},
        )
        resp = await create_upload_session(req)
        session_id = resp.headers["Location"].split("/")[-1]
        session = self._store.get(session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.name, normalized_name)
        called_ref = app[SYNOLOGY_DRIVE_API_KEY].get_file.await_args.args[0]
        self.assertEqual(called_ref.name, normalized_name)

    async def test_missing_name_raises_400(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"size": 1024},
        )
        with self.assertRaises(web.HTTPBadRequest):
            await create_upload_session(req)

    async def test_missing_size_raises_400(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"name": "file.bin"},
        )
        with self.assertRaises(web.HTTPBadRequest):
            await create_upload_session(req)

    async def test_invalid_size_raises_400(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"name": "file.bin", "size": "abc"},
        )
        with self.assertRaises(web.HTTPBadRequest):
            await create_upload_session(req)

    async def test_zero_size_raises_400(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"name": "file.bin", "size": 0},
        )
        with self.assertRaises(web.HTTPBadRequest):
            await create_upload_session(req)

    async def test_virtual_root_raises_403(self):
        from wcpan.drive.server.services.paths import SERVER_ROOT_ID

        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": SERVER_ROOT_ID},
            json_body={"name": "file.bin", "size": 1024},
        )
        with self.assertRaises(web.HTTPForbidden):
            await create_upload_session(req)

    async def test_media_params_stored_in_media_info(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"parent_id": "p1"},
            json_body={"name": "img.jpg", "size": 500, "width": 800, "height": 600},
        )
        resp = await create_upload_session(req)
        location = resp.headers["Location"]
        session_id = location.split("/")[-1]
        session = self._store.get(session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertIsNotNone(session.media_info)
        assert session.media_info is not None
        self.assertEqual(session.media_info.width, 800)
        self.assertEqual(session.media_info.height, 600)

    async def test_conflict_returns_409_with_existing_node(self):
        app = _make_app(self._store)
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
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 0,
            }
        )
        app[SYNOLOGY_DRIVE_API_KEY].get_file = AsyncMock(
            return_value={
                "file_id": "existing-1",
                "permanent_link": "existing-1",
                "parent_id": "p1",
                "display_path": "/volume1/p1/file.bin",
                "name": "file.bin",
                "type": "file",
                "content_type": "file",
                "size": 512,
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 1,
            }
        )
        req = _make_request(
            app=app,
            match_info={"parent_id": "p1"},
            json_body={"name": "file.bin", "size": 1024},
        )
        resp = await create_upload_session(req)
        self.assertEqual(resp.status, 409)
        self.assertIsNone(self._store.get("file.bin"))


# ---------- head_upload_session ----------


class TestHeadUploadSession(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_returns_200_with_upload_offset_and_length(self):
        session = self._store.create("p", "f.bin", 2048, None, None)
        session.received = 512

        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
        )
        resp = await head_upload_session(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Upload-Offset"], "512")
        self.assertEqual(resp.headers["Upload-Length"], "2048")

    async def test_nothing_received_returns_200_with_zero_offset(self):
        session = self._store.create("p", "f.bin", 2048, None, None)

        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
        )
        resp = await head_upload_session(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Upload-Offset"], "0")
        self.assertEqual(resp.headers["Upload-Length"], "2048")

    async def test_unknown_session_raises_404(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": "nonexistent"},
        )
        with self.assertRaises(web.HTTPNotFound):
            await head_upload_session(req)

    async def test_head_refreshes_session_activity(self):
        session = self._store.create("p", "f.bin", 2048, None, None)
        session.last_activity = 10.0
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
        )

        with patch(
            "wcpan.drive.server.services.upload.time.monotonic",
            return_value=20.0,
        ):
            await head_upload_session(req)

        self.assertEqual(session.last_activity, 20.0)

    async def test_expired_session_is_removed_with_temp_file(self):
        session = self._store.create("p", "f.bin", 2048, None, None)
        path = session.temp_path
        session.last_activity = 10.0

        removed = self._store.remove_expired(now=3610.0, ttl=3600.0)

        self.assertEqual(removed, 1)
        self.assertIsNone(self._store.get(session.session_id))
        self.assertFalse(path.exists())

    async def test_active_session_is_not_expired(self):
        session = self._store.create("p", "f.bin", 2048, None, None)
        session.last_activity = 10.0
        session.uploading = True

        removed = self._store.remove_expired(now=10_000.0, ttl=3600.0)

        self.assertEqual(removed, 0)
        self.assertIsNotNone(self._store.get(session.session_id))


# ---------- delete_upload_session ----------


class TestDeleteUploadSession(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    async def test_returns_204_and_removes_session(self):
        session = self._store.create("p", "f.bin", 100, None, {})
        session_id = session.session_id

        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session_id},
        )
        resp = await delete_upload_session(req)
        self.assertEqual(resp.status, 204)
        self.assertIsNone(self._store.get(session_id))

    async def test_unknown_session_raises_404(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": "nonexistent"},
        )
        with self.assertRaises(web.HTTPNotFound):
            await delete_upload_session(req)


# ---------- patch_upload_chunk ----------


class TestPatchUploadChunk(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wcpan_test_")
        self._store = UploadSessionStore(tmp_dir=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._store.close_all()
        self._tmp.cleanup()

    def _make_session(self, total: int = 100):
        return self._store.create("p1", "f.bin", total, None, None)

    async def test_partial_upload_returns_204_with_upload_offset(self):
        session = self._make_session(total=200)
        chunk = b"x" * 100  # only half the file

        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "0"},
            body=chunk,
        )
        resp = await patch_upload_chunk(req)
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.headers["Upload-Offset"], "100")

    async def test_wrong_offset_returns_409_with_upload_offset(self):
        session = self._make_session(total=200)
        session.received = 50  # server has 50 bytes

        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "0"},
            body=b"x" * 100,
        )
        resp = await patch_upload_chunk(req)
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.headers["Upload-Offset"], "50")

    async def test_missing_upload_offset_raises_400(self):
        session = self._make_session()
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={},
            body=b"x" * 50,
        )
        with self.assertRaises(web.HTTPBadRequest):
            await patch_upload_chunk(req)

    async def test_non_integer_upload_offset_raises_400(self):
        session = self._make_session()
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "abc"},
            body=b"x" * 50,
        )
        with self.assertRaises(web.HTTPBadRequest):
            await patch_upload_chunk(req)

    async def test_negative_upload_offset_raises_400(self):
        session = self._make_session()
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "-1"},
            body=b"x" * 50,
        )
        with self.assertRaises(web.HTTPBadRequest):
            await patch_upload_chunk(req)

    async def test_body_exceeding_total_size_raises_400(self):
        session = self._make_session(total=50)
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "0"},
            body=b"x" * 100,  # twice the declared size
        )
        with self.assertRaises(web.HTTPBadRequest):
            await patch_upload_chunk(req)

    async def test_unknown_session_raises_404(self):
        req = _make_request(
            app=_make_app(self._store),
            match_info={"session_id": "nonexistent"},
            headers={"Upload-Offset": "0"},
            body=b"x" * 10,
        )
        with self.assertRaises(web.HTTPNotFound):
            await patch_upload_chunk(req)

    async def test_final_chunk_triggers_synology_upload(self):
        from wcpan.drive.protocol.types import MirrorMutableId, NodeRecord

        session = self._make_session(total=50)
        chunk = b"y" * 50

        now = 1_704_067_200
        node_record = NodeRecord(
            id="new-node-1",
            parent_id="p1",
            name="f.bin",
            is_directory=False,
            created_time=now,
            modified_time=now,
            changed_time=now,
            mime_type="application/octet-stream",
            hash="abc",
            hash_algorithm="md4",
            size=50,
            is_image=False,
            is_video=False,
            width=0,
            height=0,
            ms_duration=0,
            mutable_id=MirrorMutableId("new-node-1"),
        )

        app = _make_app(self._store)
        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY],
                "upload",
                new_callable=AsyncMock,
                return_value={
                    "file_id": "new-node-1",
                    "permanent_link": "new-node-1",
                    "parent_id": "p1",
                    "name": "f.bin",
                    "type": "file",
                    "content_type": "file",
                    "size": 50,
                    "created_time": 0,
                    "modified_time": 0,
                    "change_time": 0,
                    "sync_id": 1,
                },
            ),
            patch(
                "wcpan.drive.server.handlers.lib.enrich_and_upsert_synology_node",
                new_callable=AsyncMock,
                return_value=node_record,
            ),
            patch.object(_L, "debug") as debug_log,
        ):
            req = _make_request(
                app=app,
                match_info={"session_id": session.session_id},
                headers={"Upload-Offset": "0"},
                body=chunk,
            )
            resp = await patch_upload_chunk(req)

        self.assertEqual(resp.status, 201)
        # Session should be removed after successful upload.
        self.assertIsNone(self._store.get(session.session_id))
        debug_messages = [it.args[0] for it in debug_log.call_args_list]
        self.assertIn(
            "begin finalising upload session session_id=%r name=%r parent_id=%r",
            debug_messages,
        )
        self.assertIn(
            "finalised upload session session_id=%r name=%r parent_id=%r id=%r",
            debug_messages,
        )

    async def test_finalise_failure_logs_warning_and_keeps_session(self):
        session = self._make_session(total=50)
        app = _make_app(self._store)

        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY],
                "upload",
                new_callable=AsyncMock,
                side_effect=SynologyUploadError(
                    "upload failed",
                    file_name="f.bin",
                ),
            ),
            patch.object(_L, "warning") as warning_log,
        ):
            req = _make_request(
                app=app,
                match_info={"session_id": session.session_id},
                headers={"Upload-Offset": "0"},
                body=b"y" * 50,
            )
            with self.assertRaises(web.HTTPInsufficientStorage):
                await patch_upload_chunk(req)

        self.assertIsNotNone(self._store.get(session.session_id))
        warning_log.assert_called_once()
        self.assertEqual(
            warning_log.call_args.args[0],
            "upload session permanently failed during finalisation session_id=%r name=%r parent_id=%r error=%s",
        )

    async def test_name_too_long_returns_typed_422(self):
        session = self._make_session(total=50)
        app = _make_app(self._store)
        app[SYNOLOGY_DRIVE_API_KEY].upload = AsyncMock(
            side_effect=SynologyNameTooLongError(
                "File name is too long", file_name="f.bin"
            )
        )
        req = _make_request(
            app=app,
            match_info={"session_id": session.session_id},
            headers={"Upload-Offset": "0"},
            body=b"y" * 50,
        )

        resp = await patch_upload_chunk(req)

        self.assertEqual(resp.status, 422)
        self.assertEqual(json.loads(resp.body)["error"], "name_too_long")

    async def test_finalise_network_failure_logs_warning_and_keeps_session(self):
        session = self._make_session(total=50)
        app = _make_app(self._store)

        with (
            patch.object(
                app[SYNOLOGY_DRIVE_API_KEY],
                "upload",
                new_callable=AsyncMock,
                side_effect=SynologyNetworkError("connection reset"),
            ),
            patch.object(_L, "warning") as warning_log,
        ):
            req = _make_request(
                app=app,
                match_info={"session_id": session.session_id},
                headers={"Upload-Offset": "0"},
                body=b"y" * 50,
            )
            with self.assertRaises(web.HTTPServiceUnavailable):
                await patch_upload_chunk(req)

        self.assertIsNotNone(self._store.get(session.session_id))
        warning_log.assert_called_once()
        self.assertEqual(
            warning_log.call_args.args[0],
            "upload session failed during finalisation session_id=%r name=%r parent_id=%r error=%s",
        )

    async def test_finalise_size_mismatch_logs_warning_and_keeps_session(self):
        session = self._make_session(total=50)
        app = _make_app(self._store)

        with (
            patch("pathlib.Path.stat", return_value=Mock(st_size=49)),
            patch.object(_L, "warning") as warning_log,
        ):
            req = _make_request(
                app=app,
                match_info={"session_id": session.session_id},
                headers={"Upload-Offset": "0"},
                body=b"y" * 50,
            )
            with self.assertRaises(web.HTTPInsufficientStorage) as ctx:
                await patch_upload_chunk(req)

        self.assertEqual(ctx.exception.reason, "Upload session size mismatch")
        self.assertIsNotNone(self._store.get(session.session_id))
        warning_log.assert_called_once()
        self.assertEqual(
            warning_log.call_args.args[0],
            "upload session size mismatch before finalisation session_id=%r name=%r parent_id=%r received=%d file_size=%d total_size=%d",
        )

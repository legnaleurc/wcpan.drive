"""Tests for webhook processing: classification, execution, and batch flow."""

import asyncio
import logging
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import MirrorMutableId, MirrorStableId, NodeRecord
from wcpan.drive.server.lib.mounts import MountRegistry
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.webhook import WebhookService
from wcpan.synology import (
    SynologyFileId,
    SynologyPermanentLink,
)


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


_EPOCH = 0

_FAKE_SYNO_INFO = {
    "file_id": "f1",
    "permanent_link": "f1",
    "parent_id": "p1",
    "name": "test.txt",
    "type": "file",
    "content_type": "file",
    "size": 100,
    "created_time": 1000,
    "modified_time": 2000,
    "change_time": 0,
    "sync_id": 1,
}


def _make_node(
    node_id: MirrorStableId = MirrorStableId("f1"),
    parent_id: MirrorStableId = MirrorStableId("p1"),
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name="test.txt",
        is_directory=False,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type="text/plain",
        hash="abc",
        size=100,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        mutable_id=MirrorMutableId(str(node_id)),
    )


def _make_service(
    drive_api: object | None = None,
    storage: object | None = None,
    node_sync: object | None = None,
    syno_paths: object | None = None,
    write_queue: asyncio.Queue | None = None,
    mount_registry: MountRegistry | None = None,
) -> WebhookService:
    return WebhookService(
        drive_api=drive_api or MagicMock(),
        storage=storage or MagicMock(),
        node_sync=node_sync or MagicMock(spec=NodeSyncService),
        syno_paths=syno_paths or MagicMock(),
        write_queue=write_queue or asyncio.Queue(),
        mount_registry=mount_registry or MountRegistry(mounts={}, root_ids={}),
    )


# ---------------------------------------------------------------------------
# _fetch_and_enrich
# ---------------------------------------------------------------------------


class TestFetchAndEnrich(IsolatedAsyncioTestCase):
    async def test_success(self):
        drive_api = MagicMock()
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=_make_node("p1"))
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.upsert = AsyncMock(return_value=_make_node())
        drive_api.get_file = AsyncMock(return_value=_FAKE_SYNO_INFO)
        service = _make_service(drive_api, storage, node_sync)

        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="p1"),
            "file_created",
        )
        node_sync.upsert.assert_awaited_once()

    async def test_no_parent_id_skips(self):
        service = _make_service()
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            None,
            "file_created",
        )

    async def test_parent_not_in_db_not_mount_skips(self):
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        service = _make_service(storage=storage)
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_created",
        )

    async def test_file_moved_to_unknown_parent_deletes_existing_node(self):
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        storage.get_node_by_id = AsyncMock(return_value=_make_node())
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.delete = AsyncMock()
        service = _make_service(storage=storage, node_sync=node_sync)

        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_moved",
        )
        node_sync.delete.assert_awaited_once_with(MirrorStableId("f1"))

    async def test_file_moved_to_unknown_parent_no_op_when_node_absent(self):
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        storage.get_node_by_id = AsyncMock(return_value=None)
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.delete = AsyncMock()
        service = _make_service(storage=storage, node_sync=node_sync)

        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_moved",
        )
        node_sync.delete.assert_not_awaited()

    async def test_parent_resolved_via_mount(self):
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.upsert = AsyncMock(return_value=_make_node())
        registry = MountRegistry(
            mounts={},
            root_ids={
                SynologyFileId(file_id="real-parent"): MirrorStableId("_docs"),
            },
        )
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(return_value=_FAKE_SYNO_INFO)
        service = _make_service(drive_api, storage, node_sync, mount_registry=registry)

        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="real-parent"),
            "file_created",
        )
        # parent_id should have been resolved to _docs
        call_args = node_sync.upsert.call_args[0][0]
        self.assertEqual(call_args.parent_id, "_docs")

    async def test_metadata_not_found_skips(self):
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=_make_node("p1"))
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(return_value=None)
        service = _make_service(drive_api, storage)

        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="p1"),
            "file_created",
        )


# ---------------------------------------------------------------------------
# _classify_webhook_item
# ---------------------------------------------------------------------------


class TestClassifyWebhookItem(IsolatedAsyncioTestCase):
    def _service(self):
        drive_api = MagicMock()
        storage = MagicMock()
        storage.get_node_by_mutable_id = AsyncMock(return_value=_make_node("p1"))
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.upsert = AsyncMock(return_value=_make_node())
        drive_api.get_file = AsyncMock(return_value=_FAKE_SYNO_INFO)
        registry = MountRegistry(mounts={}, root_ids={})
        return _make_service(drive_api, storage, node_sync, mount_registry=registry)

    async def test_empty_file_id_schedules_delayed_upsert(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertEqual(plan.schedule_key, "f1")
        self.assertEqual(plan.file_ref.file_id, "")
        self.assertTrue(plan.schedule_delayed_upsert)

    async def test_file_removed_returns_delete_id(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_removed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertEqual(plan.delete_id, "f1")
        self.assertFalse(plan.schedule_delayed_upsert)

    async def test_file_modified_file_schedules_delayed(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertTrue(plan.schedule_delayed_upsert)
        self.assertFalse(plan.fetch_immediately)

    async def test_file_modified_dir_is_noop(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        self.assertIsNone(plan.delete_id)
        self.assertFalse(plan.fetch_immediately)
        self.assertFalse(plan.schedule_delayed_upsert)

    async def test_file_created_dir_fetches_and_waits(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        self.assertTrue(plan.fetch_immediately)
        self.assertTrue(plan.wait_for_writes)
        self.assertFalse(plan.scan_moved_dir_subtree)

    async def test_file_moved_dir_fetches_waits_and_scans(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        self.assertTrue(plan.fetch_immediately)
        self.assertTrue(plan.wait_for_writes)
        self.assertTrue(plan.scan_moved_dir_subtree)

    async def test_file_renamed_fetches_immediately(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_renamed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertTrue(plan.fetch_immediately)
        self.assertFalse(plan.wait_for_writes)
        self.assertFalse(plan.scan_moved_dir_subtree)

    async def test_file_created_file_schedules_delayed(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertIsNone(plan.delete_id)
        self.assertTrue(plan.schedule_delayed_upsert)

    async def test_unknown_event_type_is_noop(self):
        service = self._service()
        plan = service._classify_webhook_item(
            {
                "event_type": "something_else",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        self.assertIsNone(plan.delete_id)
        self.assertFalse(plan.fetch_immediately)
        self.assertFalse(plan.schedule_delayed_upsert)


# ---------------------------------------------------------------------------
# _execute_webhook_plan
# ---------------------------------------------------------------------------


class TestExecuteWebhookPlan(IsolatedAsyncioTestCase):
    async def test_fetches_then_waits_for_created_dir(self):
        service = _make_service()
        pending = MagicMock()
        service._write_queue.join = AsyncMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with patch.object(
            service,
            "_fetch_and_enrich",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await service._execute_webhook_plan(plan, pending=pending)

        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        service._write_queue.join.assert_awaited_once()
        pending.schedule.assert_not_called()

    async def test_deletes_when_plan_requests_removal(self):
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.delete = AsyncMock()
        service = _make_service(node_sync=node_sync)
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_removed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )

        await service._execute_webhook_plan(plan, pending=pending)

        node_sync.delete.assert_awaited_once_with("f1")
        pending.schedule.assert_not_called()

    async def test_schedules_delayed_upsert(self):
        service = _make_service()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )

        with patch.object(
            service,
            "_fetch_and_enrich",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await service._execute_webhook_plan(plan, pending=pending)

            pending.schedule.assert_called_once()
            self.assertEqual(pending.schedule.call_args.args[0], "f1")

            factory = pending.schedule.call_args.args[1]
            await factory()
            mock_fetch.assert_awaited_once_with(
                plan.file_ref,
                plan.permanent_link_ref,
                plan.parent_file_ref,
                plan.event_type,
            )

    async def test_mirrored_directory_move_fetches_waits_without_scanning(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("f1", "_video"))
        storage.get_node_by_mutable_id = AsyncMock(
            return_value=_make_node("p1", "_video")
        )
        registry = MountRegistry(mounts={"video": "/team-folders/video/L"}, root_ids={})
        service = _make_service(storage=storage, mount_registry=registry)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with (
            patch.object(
                service,
                "_fetch_and_enrich",
                new_callable=AsyncMock,
            ) as mock_fetch,
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        service._write_queue.join.assert_awaited_once()
        mock_scan.assert_not_awaited()

    async def test_mirrored_directory_move_to_mount_root_skips_scanning(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("f1", "_video"))
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        registry = MountRegistry(
            mounts={"video": "/team-folders/video/L"},
            root_ids={
                SynologyFileId(file_id="mount-root"): MirrorStableId("_video"),
            },
        )
        service = _make_service(storage=storage, mount_registry=registry)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "mount-root",
            },
        )

        with (
            patch.object(
                service,
                "_fetch_and_enrich",
                new_callable=AsyncMock,
            ) as mock_fetch,
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_fetch.assert_awaited_once()
        service._write_queue.join.assert_awaited_once()
        mock_scan.assert_not_awaited()

    async def test_moved_directory_source_not_under_current_mount_scans(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("f1", "_old"))
        storage.get_node_by_mutable_id = AsyncMock(
            return_value=_make_node("p1", "_video")
        )
        registry = MountRegistry(mounts={"video": "/team-folders/video/L"}, root_ids={})
        service = _make_service(storage=storage, mount_registry=registry)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with (
            patch.object(service, "_fetch_and_enrich", new_callable=AsyncMock),
            patch.object(
                service,
                "_resolve_moved_dir_root_id",
                new_callable=AsyncMock,
                return_value=MirrorStableId("f1"),
            ),
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_scan.assert_awaited_once_with(MirrorStableId("f1"))

    async def test_moved_directory_destination_not_under_current_mount_scans(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("f1", "_video"))
        storage.get_node_by_mutable_id = AsyncMock(
            return_value=_make_node("p1", "_old")
        )
        registry = MountRegistry(mounts={"video": "/team-folders/video/L"}, root_ids={})
        service = _make_service(storage=storage, mount_registry=registry)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with (
            patch.object(service, "_fetch_and_enrich", new_callable=AsyncMock),
            patch.object(
                service,
                "_resolve_moved_dir_root_id",
                new_callable=AsyncMock,
                return_value=MirrorStableId("f1"),
            ),
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_scan.assert_awaited_once_with(MirrorStableId("f1"))

    async def test_moved_directory_missing_source_scans(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=None)
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        service = _make_service(storage=storage)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with (
            patch.object(service, "_fetch_and_enrich", new_callable=AsyncMock),
            patch.object(
                service,
                "_resolve_moved_dir_root_id",
                new_callable=AsyncMock,
                return_value=MirrorStableId("f1"),
            ),
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_scan.assert_awaited_once_with(MirrorStableId("f1"))

    async def test_moved_directory_missing_destination_scans(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("f1", "_video"))
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        registry = MountRegistry(mounts={"video": "/team-folders/video/L"}, root_ids={})
        service = _make_service(storage=storage, mount_registry=registry)
        service._write_queue.join = AsyncMock()
        pending = MagicMock()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        with (
            patch.object(service, "_fetch_and_enrich", new_callable=AsyncMock),
            patch.object(
                service,
                "_resolve_moved_dir_root_id",
                new_callable=AsyncMock,
                return_value=MirrorStableId("f1"),
            ),
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=pending)

        mock_scan.assert_awaited_once_with(MirrorStableId("f1"))


# ---------------------------------------------------------------------------
# _resolve_moved_dir_root_id
# ---------------------------------------------------------------------------


class TestResolveMovedDirRootId(IsolatedAsyncioTestCase):
    async def test_uses_permanent_link_record(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=_make_node("perm-1"))
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        drive_api = MagicMock()
        service = _make_service(drive_api, storage)

        rv = await service._resolve_moved_dir_root_id(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="perm-1"),
        )

        self.assertEqual(rv, "perm-1")
        drive_api.get_file.assert_not_called()
        storage.get_node_by_mutable_id.assert_not_awaited()

    async def test_falls_back_to_metadata_permanent_link(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=None)
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(
            return_value={**_FAKE_SYNO_INFO, "permanent_link": "perm-2"}
        )
        service = _make_service(drive_api, storage)

        rv = await service._resolve_moved_dir_root_id(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="perm-1"),
        )

        self.assertEqual(rv, "perm-2")
        storage.get_node_by_mutable_id.assert_not_awaited()

    async def test_falls_back_to_storage_lookup_by_file_id(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=None)
        storage.get_node_by_mutable_id = AsyncMock(return_value=_make_node("mirror-1"))
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(return_value=None)
        service = _make_service(drive_api, storage)

        rv = await service._resolve_moved_dir_root_id(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="perm-1"),
        )

        self.assertEqual(rv, "mirror-1")
        storage.get_node_by_mutable_id.assert_awaited_once_with(MirrorMutableId("f1"))

    async def test_returns_none_when_unresolved(self):
        storage = MagicMock()
        storage.get_node_by_id = AsyncMock(return_value=None)
        storage.get_node_by_mutable_id = AsyncMock(return_value=None)
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(return_value=None)
        service = _make_service(drive_api, storage)

        rv = await service._resolve_moved_dir_root_id(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="perm-1"),
        )

        self.assertIsNone(rv)


class TestWebhookServiceMovedDir(IsolatedAsyncioTestCase):
    async def test_skips_subtree_scan_when_root_unresolved(self):
        drive_api = MagicMock()
        storage = MagicMock()

        async def _get_by_mutable_id(mutable_id: MirrorMutableId):
            if mutable_id == MirrorMutableId("p1"):
                return _make_node("p1")
            return None

        storage.get_node_by_mutable_id = AsyncMock(side_effect=_get_by_mutable_id)
        storage.get_node_by_id = AsyncMock(return_value=None)
        node_sync = MagicMock(spec=NodeSyncService)
        node_sync.upsert = AsyncMock(return_value=_make_node())
        node_sync.delete = AsyncMock()
        syno_paths = MagicMock()
        write_queue = asyncio.Queue()
        mount_registry = MountRegistry(mounts={}, root_ids={})
        service = WebhookService(
            drive_api=drive_api,
            storage=storage,
            node_sync=node_sync,
            syno_paths=syno_paths,
            write_queue=write_queue,
            mount_registry=mount_registry,
        )
        queue = asyncio.Queue()
        await queue.put(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            }
        )
        scan_done_event = asyncio.Event()
        scan_done_event.set()

        drive_api.get_file = AsyncMock(return_value=None)

        with patch.object(
            service,
            "_scan_moved_dir_subtree",
            new_callable=AsyncMock,
        ) as mock_scan:

            async def _run_once():
                async with asyncio.TaskGroup() as group:
                    group.create_task(service.run(queue, group, scan_done_event))
                    await queue.join()
                    raise asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await _run_once()

        mock_scan.assert_not_awaited()

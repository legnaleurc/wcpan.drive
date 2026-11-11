"""Tests for webhook processing: classification, execution, and batch flow."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from decoy import matchers
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.lib.debounce import TaskIdDebouncer
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._store import SynologyStore
from wcpan.drive.server.synology._webhook import WebhookService
from wcpan.drive.server.types import NodeObservation, ReadSeq
from wcpan.synology import (
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyPermanentLink,
)

from ._lib import DecoyTestCase


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


_EPOCH = 0
_READ = ReadSeq(1)

_FAKE_SYNO_INFO: SynologyFileInfo = {
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

_VIDEO_MOUNT = MountRegistry(
    mounts=Mounts.from_sources({"video": "/team-folders/video/L"}), root_ids={}
)


def _make_node(
    node_id: NodeId = NodeId("f1"),
    parent_id: NodeId = NodeId("p1"),
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
        hash_algorithm="md4",
        size=100,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class _WebhookTestCase(DecoyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.mirror = self.decoy.mock(cls=MirrorStore)
        self.store = self.decoy.mock(cls=SynologyStore)
        self.node_sync = self.decoy.mock(cls=NodeSyncService)
        self.syno_paths = self.decoy.mock(cls=SynologyPathService)
        self.pending = self.decoy.mock(cls=TaskIdDebouncer)
        self.decoy.when(self.node_sync.observe()).then_enter_with(_READ)

    def _create_service(
        self, mount_registry: MountRegistry | None = None
    ) -> WebhookService:
        return WebhookService(
            drive_api=self.drive_api,
            mirror=self.mirror,
            store=self.store,
            node_sync=self.node_sync,
            syno_paths=self.syno_paths,
            mount_registry=mount_registry
            or MountRegistry(mounts=Mounts.from_sources({}), root_ids={}),
        )

    async def _stub_source(self, node: NodeRecord) -> None:
        self.decoy.when(await self.mirror.get_node_by_id(NodeId("f1"))).then_return(
            node
        )

    async def _stub_parent(self, node: NodeRecord) -> None:
        self.decoy.when(
            await self.store.get_node_by_locator(SynologyLocator("p1"))
        ).then_return(node)


# ---------------------------------------------------------------------------
# _fetch_and_enrich
# ---------------------------------------------------------------------------


class TestFetchAndEnrich(_WebhookTestCase):
    async def test_success(self):
        # given
        await self._stub_parent(_make_node("p1"))
        self.decoy.when(
            await self.drive_api.get_file(SynologyPermanentLink(permanent_link="f1"))
        ).then_return(_FAKE_SYNO_INFO)
        service = self._create_service()

        # when
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="p1"),
            "file_created",
        )
        # then
        self.decoy.verify(
            await self.node_sync.upsert(matchers.Anything(), read=_READ), times=1
        )

    async def test_no_parent_id_skips(self):
        # given
        service = self._create_service()
        # when / then
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            None,
            "file_created",
        )

    async def test_parent_not_in_db_not_mount_skips(self):
        # given
        service = self._create_service()
        # when / then
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_created",
        )

    async def test_file_moved_to_unknown_parent_deletes_existing_node(self):
        # given
        await self._stub_source(_make_node())
        service = self._create_service()

        # when
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_moved",
        )
        # then
        self.decoy.verify(await self.node_sync.delete(NodeId("f1"), read=_READ))

    async def test_file_moved_to_unknown_parent_no_op_when_node_absent(self):
        # given
        service = self._create_service()

        # when
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="unknown-parent"),
            "file_moved",
        )
        # then
        self.decoy.verify(
            await self.node_sync.delete(matchers.Anything(), read=matchers.Anything()),
            times=0,
        )

    async def test_parent_resolved_via_mount(self):
        # given
        registry = MountRegistry(
            mounts=Mounts.from_sources({}),
            root_ids={
                SynologyFileId(file_id="real-parent"): NodeId("_docs"),
            },
        )
        self.decoy.when(
            await self.drive_api.get_file(SynologyPermanentLink(permanent_link="f1"))
        ).then_return(_FAKE_SYNO_INFO)
        service = self._create_service(registry)

        # when
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="real-parent"),
            "file_created",
        )
        # then
        # parent_id should have been resolved to _docs
        observation = matchers.Captor()
        self.decoy.verify(await self.node_sync.upsert(observation, read=_READ))
        upserted: NodeObservation = observation.value
        self.assertEqual(upserted.record.parent_id, "_docs")
        self.assertEqual(upserted.locator, "f1")

    async def test_metadata_not_found_skips(self):
        # given
        await self._stub_parent(_make_node("p1"))
        service = self._create_service()

        # when
        await service._fetch_and_enrich(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="f1"),
            SynologyFileId(file_id="p1"),
            "file_created",
        )
        # then
        self.decoy.verify(
            await self.node_sync.upsert(matchers.Anything(), read=matchers.Anything()),
            times=0,
        )


# ---------------------------------------------------------------------------
# _classify_webhook_item
# ---------------------------------------------------------------------------


class TestClassifyWebhookItem(_WebhookTestCase):
    async def test_empty_file_id_schedules_delayed_upsert(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertEqual(plan.schedule_key, "f1")
        self.assertEqual(plan.file_ref.file_id, "")
        self.assertTrue(plan.schedule_delayed_upsert)

    async def test_file_removed_returns_delete_id(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_removed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertEqual(plan.delete_id, "f1")
        self.assertFalse(plan.schedule_delayed_upsert)

    async def test_file_modified_file_schedules_delayed(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertTrue(plan.schedule_delayed_upsert)
        self.assertFalse(plan.fetch_immediately)

    async def test_file_modified_dir_is_noop(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        # then
        self.assertIsNone(plan.delete_id)
        self.assertFalse(plan.fetch_immediately)
        self.assertFalse(plan.schedule_delayed_upsert)

    async def test_file_created_dir_fetches_and_waits(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        # then
        self.assertTrue(plan.fetch_immediately)
        self.assertTrue(plan.wait_for_writes)
        self.assertFalse(plan.scan_moved_dir_subtree)

    async def test_file_moved_dir_fetches_waits_and_scans(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_moved",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )
        # then
        self.assertTrue(plan.fetch_immediately)
        self.assertTrue(plan.wait_for_writes)
        self.assertTrue(plan.scan_moved_dir_subtree)

    async def test_file_renamed_fetches_immediately(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_renamed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertTrue(plan.fetch_immediately)
        self.assertFalse(plan.wait_for_writes)
        self.assertFalse(plan.scan_moved_dir_subtree)

    async def test_file_created_file_schedules_delayed(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertIsNone(plan.delete_id)
        self.assertTrue(plan.schedule_delayed_upsert)

    async def test_unknown_event_type_is_noop(self):
        # given
        service = self._create_service()
        # when
        plan = service._classify_webhook_item(
            {
                "event_type": "something_else",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )
        # then
        self.assertIsNone(plan.delete_id)
        self.assertFalse(plan.fetch_immediately)
        self.assertFalse(plan.schedule_delayed_upsert)


# ---------------------------------------------------------------------------
# _execute_webhook_plan
# ---------------------------------------------------------------------------


def _moved_dir(parent_id: str = "p1"):
    return {
        "event_type": "file_moved",
        "file_id": "f1",
        "permanent_link": "f1",
        "file_type": "dir",
        "parent_id": parent_id,
    }


class TestExecuteWebhookPlan(_WebhookTestCase):
    def _verify_not_scheduled(self) -> None:
        self.decoy.verify(
            self.pending.schedule(matchers.Anything(), matchers.Anything()), times=0
        )

    async def test_fetches_then_waits_for_created_dir(self):
        # given
        service = self._create_service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_created",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "dir",
                "parent_id": "p1",
            },
        )

        # when
        with patch.object(
            service,
            "_fetch_and_enrich",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await service._execute_webhook_plan(plan, pending=self.pending)

        # then
        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        self.decoy.verify(await self.node_sync.flush(), times=1)
        self._verify_not_scheduled()

    async def test_deletes_when_plan_requests_removal(self):
        # given
        service = self._create_service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_removed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )

        # when
        await service._execute_webhook_plan(plan, pending=self.pending)

        # then
        self.decoy.verify(
            await self.node_sync.delete(NodeId("f1"), read=_READ), times=1
        )
        self._verify_not_scheduled()

    async def test_schedules_delayed_upsert(self):
        # given
        service = self._create_service()
        plan = service._classify_webhook_item(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            },
        )

        # when
        with patch.object(
            service,
            "_fetch_and_enrich",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await service._execute_webhook_plan(plan, pending=self.pending)

            # then
            factory = matchers.Captor()
            self.decoy.verify(self.pending.schedule("f1", factory), times=1)

            # when
            await factory.value()
            # then
            mock_fetch.assert_awaited_once_with(
                plan.file_ref,
                plan.permanent_link_ref,
                plan.parent_file_ref,
                plan.event_type,
            )

    async def test_mirrored_directory_move_fetches_waits_without_scanning(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_video")))
        await self._stub_parent(_make_node("p1", NodeId("_video")))
        service = self._create_service(_VIDEO_MOUNT)
        plan = service._classify_webhook_item(_moved_dir())

        # when
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
            await service._execute_webhook_plan(plan, pending=self.pending)

        # then
        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        self.decoy.verify(await self.node_sync.flush(), times=1)
        mock_scan.assert_not_awaited()

    async def test_mirrored_directory_move_to_mount_root_skips_scanning(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_video")))
        registry = MountRegistry(
            mounts=Mounts.from_sources({"video": "/team-folders/video/L"}),
            root_ids={
                SynologyFileId(file_id="mount-root"): NodeId("_video"),
            },
        )
        service = self._create_service(registry)
        plan = service._classify_webhook_item(_moved_dir("mount-root"))

        # when
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
            await service._execute_webhook_plan(plan, pending=self.pending)

        # then
        mock_fetch.assert_awaited_once()
        self.decoy.verify(await self.node_sync.flush(), times=1)
        mock_scan.assert_not_awaited()

    async def _assert_scans(self, service: WebhookService) -> None:
        plan = service._classify_webhook_item(_moved_dir())

        with (
            patch.object(service, "_fetch_and_enrich", new_callable=AsyncMock),
            patch.object(
                service,
                "_resolve_moved_dir_root_id",
                new_callable=AsyncMock,
                return_value=NodeId("f1"),
            ),
            patch.object(
                service,
                "_scan_moved_dir_subtree",
                new_callable=AsyncMock,
            ) as mock_scan,
        ):
            await service._execute_webhook_plan(plan, pending=self.pending)

        mock_scan.assert_awaited_once_with(NodeId("f1"))

    async def test_moved_directory_source_not_under_current_mount_scans(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_old")))
        await self._stub_parent(_make_node("p1", NodeId("_video")))
        # when / then
        await self._assert_scans(self._create_service(_VIDEO_MOUNT))

    async def test_moved_directory_destination_not_under_current_mount_scans(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_video")))
        await self._stub_parent(_make_node("p1", NodeId("_old")))
        # when / then
        await self._assert_scans(self._create_service(_VIDEO_MOUNT))

    async def test_moved_directory_missing_source_scans(self):
        # when / then
        await self._assert_scans(self._create_service())

    async def test_moved_directory_missing_destination_scans(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_video")))
        # when / then
        await self._assert_scans(self._create_service(_VIDEO_MOUNT))


# ---------------------------------------------------------------------------
# _resolve_moved_dir_root_id
# ---------------------------------------------------------------------------


class TestResolveMovedDirRootId(_WebhookTestCase):
    async def _resolve(self) -> NodeId | None:
        return await self._create_service()._resolve_moved_dir_root_id(
            SynologyFileId(file_id="f1"),
            SynologyPermanentLink(permanent_link="perm-1"),
        )

    async def _verify_no_locator_lookup(self) -> None:
        self.decoy.verify(
            await self.store.get_node_by_locator(matchers.Anything()), times=0
        )

    async def test_uses_permanent_link_record(self):
        # given
        self.decoy.when(await self.mirror.get_node_by_id(NodeId("perm-1"))).then_return(
            _make_node(NodeId("perm-1"))
        )

        # when
        rv = await self._resolve()

        # then
        self.assertEqual(rv, "perm-1")
        self.decoy.verify(await self.drive_api.get_file(matchers.Anything()), times=0)
        await self._verify_no_locator_lookup()

    async def test_falls_back_to_metadata_permanent_link(self):
        # given
        self.decoy.when(
            await self.drive_api.get_file(
                SynologyPermanentLink(permanent_link="perm-1")
            )
        ).then_return({**_FAKE_SYNO_INFO, "permanent_link": "perm-2"})

        # when
        rv = await self._resolve()

        # then
        self.assertEqual(rv, "perm-2")
        await self._verify_no_locator_lookup()

    async def test_falls_back_to_storage_lookup_by_file_id(self):
        # given
        self.decoy.when(
            await self.store.get_node_by_locator(SynologyLocator("f1"))
        ).then_return(_make_node(NodeId("mirror-1")))

        # when
        rv = await self._resolve()

        # then
        self.assertEqual(rv, "mirror-1")

    async def test_returns_none_when_unresolved(self):
        # when
        rv = await self._resolve()

        # then
        self.assertIsNone(rv)


class TestWebhookServiceMovedDir(_WebhookTestCase):
    async def test_skips_subtree_scan_when_root_unresolved(self):
        # given
        await self._stub_parent(_make_node("p1"))
        service = self._create_service()
        queue = asyncio.Queue()
        await queue.put(_moved_dir())
        scan_done_event = asyncio.Event()
        scan_done_event.set()

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

            # when
            with self.assertRaises(asyncio.CancelledError):
                await _run_once()

        # then
        mock_scan.assert_not_awaited()

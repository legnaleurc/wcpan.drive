"""Tests for webhook processing: classification, execution, and batch flow."""

import asyncio
import logging
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from decoy import matchers
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.backend.synology._backend import SynologyBackend, _Serving
from wcpan.drive.server.backend.synology._convert import SynologyLocator
from wcpan.drive.server.backend.synology._mounts import MountRegistry
from wcpan.drive.server.backend.synology._paths import SynologyPathService
from wcpan.drive.server.backend.synology._store import SynologyStore
from wcpan.drive.server.backend.synology._webhook import (
    PENDING_FILE_DELAY,
    WebhookService,
    _SubtreeLister,
)
from wcpan.drive.server.testing import BackendLifecycle
from wcpan.drive.server.types import (
    ChangeHandler,
    Changes,
    CoreServices,
    HintStage,
    MirrorReader,
    Mounts,
    NodeObservation,
    ScanRunner,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyPermanentLink,
)

from ._lib import ROOT_ID, DecoyTestCase, IdentityIds, make_mounts


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


_EPOCH = 0

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
    mounts=make_mounts({"video": "/team-folders/video/L"}), root_ids={}
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
    )


class _WebhookTestCase(DecoyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.mirror = self.decoy.mock(cls=MirrorReader)
        self.store = self.decoy.mock(cls=SynologyStore)
        self.scans = self.decoy.mock(cls=ScanRunner)
        self.changes = self.decoy.mock(cls=Changes)
        self.hints = self.decoy.mock(cls=HintStage)
        self.decoy.when(self.scans.observe()).then_enter_with(self.changes)

    def _create_service(
        self,
        mount_registry: MountRegistry | None = None,
        *,
        hints: HintStage | None = None,
    ) -> WebhookService:
        registry = mount_registry or MountRegistry(
            mounts=Mounts(root_id=ROOT_ID), root_ids={}
        )
        return WebhookService(
            drive_api=self.drive_api,
            mirror=self.mirror,
            store=self.store,
            scans=self.scans,
            syno_paths=SynologyPathService(
                registry=registry, store=self.store, ids=IdentityIds()
            ),
            mount_registry=registry,
            hints=hints or self.hints,
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
            await self.changes.upserted(matchers.Anything()),
            times=1,
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
        self.decoy.verify(await self.changes.removed(NodeId("f1")))

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
            await self.changes.removed(matchers.Anything()),
            times=0,
        )

    async def test_parent_resolved_via_mount(self):
        # given
        registry = MountRegistry(
            mounts=Mounts(root_id=ROOT_ID),
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
        self.decoy.verify(await self.changes.upserted(observation))
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
            await self.changes.upserted(matchers.Anything()),
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

    async def test_file_removed_returns_delete_link(self):
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
        self.assertEqual(plan.delete_link, "f1")
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
        self.assertIsNone(plan.delete_link)
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
        self.assertIsNone(plan.delete_link)
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
        self.assertIsNone(plan.delete_link)
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
            self.hints.later(
                matchers.Anything(), matchers.Anything(), matchers.Anything()
            ),
            times=0,
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
            await service._execute_webhook_plan(plan)

        # then
        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        self.decoy.verify(await self.changes.landed(), times=1)
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
        await service._execute_webhook_plan(plan)

        # then
        self.decoy.verify(await self.changes.removed(NodeId("f1")), times=1)
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
            await service._execute_webhook_plan(plan)

            # then
            factory = matchers.Captor()
            self.decoy.verify(
                self.hints.later("f1", PENDING_FILE_DELAY, factory), times=1
            )

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
            await service._execute_webhook_plan(plan)

        # then
        mock_fetch.assert_awaited_once_with(
            plan.file_ref,
            plan.permanent_link_ref,
            plan.parent_file_ref,
            plan.event_type,
        )
        self.decoy.verify(await self.changes.landed(), times=1)
        mock_scan.assert_not_awaited()

    async def test_mirrored_directory_move_to_mount_root_skips_scanning(self):
        # given
        await self._stub_source(_make_node("f1", NodeId("_video")))
        registry = MountRegistry(
            mounts=make_mounts({"video": "/team-folders/video/L"}),
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
            await service._execute_webhook_plan(plan)

        # then
        mock_fetch.assert_awaited_once()
        self.decoy.verify(await self.changes.landed(), times=1)
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
            await service._execute_webhook_plan(plan)

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
        hints = HintStage()
        service = self._create_service(hints=hints)
        hints.publish(_moved_dir())

        with patch.object(
            service,
            "_scan_moved_dir_subtree",
            new_callable=AsyncMock,
        ) as mock_scan:
            # when
            consumer = asyncio.create_task(hints.run(service))
            try:
                async with asyncio.timeout(5):
                    await hints.join()
            finally:
                consumer.cancel()

        # then
        mock_scan.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failures propagate (let it crash)
# ---------------------------------------------------------------------------


class TestWebhookFailuresPropagate(_WebhookTestCase):
    """A failing NAS call stops the service; the next initial scan repairs."""

    async def test_run_stops_when_an_event_fails(self):
        # given
        hints = HintStage()
        service = self._create_service(hints=hints)
        hints.publish(
            {
                "event_type": "file_renamed",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            }
        )

        # when
        with patch.object(
            service,
            "_fetch_and_enrich",
            new=AsyncMock(side_effect=OSError("NAS unavailable")),
        ):
            with self.assertRaisesRegex(OSError, "NAS unavailable"):
                async with asyncio.timeout(5):
                    await hints.run(service)

        # then: the event is not left unfinished
        async with asyncio.timeout(5):
            await hints.join()

    async def test_delayed_update_failure_stops_the_consumer(self):
        # given
        clock: list[asyncio.Future[None]] = []

        async def sleep(_: float) -> None:
            future = asyncio.get_running_loop().create_future()
            clock.append(future)
            await future

        hints = HintStage(sleep=sleep)
        service = self._create_service(hints=hints)
        hints.publish(
            {
                "event_type": "file_modified",
                "file_id": "f1",
                "permanent_link": "f1",
                "file_type": "file",
                "parent_id": "p1",
            }
        )
        with patch.object(
            service,
            "_fetch_and_enrich",
            new=AsyncMock(side_effect=OSError("NAS unavailable")),
        ):
            consumer = asyncio.create_task(hints.run(service))
            async with asyncio.timeout(5):
                await hints.join()
                while not clock:
                    await asyncio.sleep(0)
            # when
            clock[0].set_result(None)
            # then
            with self.assertRaisesRegex(OSError, "NAS unavailable"):
                async with asyncio.timeout(5):
                    await consumer

    async def test_moved_dir_scan_raises_when_listing_fails(self):
        # given
        service = self._create_service()
        self.decoy.when(
            await self.scans.walk(matchers.Anything(), NodeId("d1"))
        ).then_raise(OSError("NAS unavailable"))

        # when / then
        with self.assertRaisesRegex(OSError, "NAS unavailable"):
            await service._scan_moved_dir_subtree(NodeId("d1"))


class TestSubtreeLister(IsolatedAsyncioTestCase):
    async def test_lists_every_child_and_descends_into_folders(self) -> None:
        # given
        folder = {"type": "dir"}
        file = {"type": "file"}
        folder_fact = NodeObservation(record=_make_node(NodeId("d")), locator=None)
        file_fact = NodeObservation(record=_make_node(NodeId("f")), locator=None)
        paths: Any = MagicMock()
        paths.list_children = AsyncMock(return_value=[folder, file])
        paths.convert = AsyncMock(
            return_value=[(folder, folder_fact), (file, file_fact)]
        )
        lister = _SubtreeLister(MagicMock(), paths)
        # when
        listing = await lister.list_folder(NodeId("root"))
        # then
        self.assertEqual(listing.changed, [folder_fact, file_fact])
        self.assertEqual(listing.seen, [NodeId("d"), NodeId("f")])
        self.assertEqual(listing.subfolders, [NodeId("d")])


# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------


class _Scan:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def run_until_ready(self) -> None:
        await self.release.wait()


class _Handler(ChangeHandler[str]):
    def __init__(self) -> None:
        self.started: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, event: str) -> None:
        self.started.append(event)
        self.entered.set()
        await self.release.wait()


class TestWebhookLifecycle(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        context: Any = MagicMock()
        node_sync: Any = MagicMock()
        node_sync.wait_enrichment_drained = AsyncMock()
        self.core = CoreServices(
            mirror=MagicMock(),
            node_sync=node_sync,
            ids=IdentityIds(),
            media=MagicMock(),
        )
        context.core = self.core
        self.hints = self.core.hints
        self.scan = _Scan()
        self.handler = _Handler()
        self.backend = SynologyBackend(
            context=context, config=MagicMock(), ns=MagicMock()
        )
        serving: Any = _Serving(
            operations=MagicMock(),
            uploader=MagicMock(),
            files=MagicMock(),
            startup_scan=self.scan,  # pyright: ignore[reportArgumentType]
            webhook_service=self.handler,  # pyright: ignore[reportArgumentType]
        )
        self.backend._serving = serving

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_webhooks_wait_for_the_scan_and_ready_waits_for_them(self):
        # given: a webhook arrives during the startup scan
        lifecycle = BackendLifecycle(self.backend, self.core)
        running = asyncio.create_task(lifecycle.run())
        ready = asyncio.create_task(lifecycle.until_ready())
        self.addCleanup(running.cancel)
        self.addCleanup(ready.cancel)
        self.hints.publish("event")
        await self.settle()
        handled_during_scan = list(self.handler.started)
        # when: the scan ends
        self.scan.release.set()
        async with asyncio.timeout(5):
            await self.handler.entered.wait()
        await self.settle()
        ready_while_handling = ready.done()
        self.handler.release.set()
        async with asyncio.timeout(5):
            await ready
        # then
        self.assertEqual(handled_during_scan, [])
        self.assertFalse(ready_while_handling)
        self.assertEqual(self.handler.started, ["event"])

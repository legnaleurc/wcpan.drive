"""Tests for scanner write-back of per-mount state to database."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend.synology._mounts import MountRegistry
from wcpan.drive.server.backend.synology._paths import SynologyPathService
from wcpan.drive.server.backend.synology._scan import (
    StartupScanService,
    _Folder,
    _MountPass,
)
from wcpan.drive.server.backend.synology._store import (
    SYNOLOGY_SCHEMA,
    SynologyStore,
    SynologyStoreReader,
)
from wcpan.drive.server.exceptions import ListingUnavailableError
from wcpan.drive.server.testing import BackendInstance, backend_harness
from wcpan.synology.exceptions import SynologyNetworkError
from wcpan.synology.types import (
    SynologyClient,
    SynologyPath,
)

from ._lib import DecoyTestCase, IdentityIds, make_mounts, mounts_of


class TestScannerWriteBack(DecoyTestCase):
    async def _run_initial_sync(
        self,
        instance: BackendInstance,
        mirror: Any,
        mounts: dict[str, SynologyPath],
        checkpoints: dict[str, int],
    ) -> None:
        core = instance.core
        store = SynologyStore(
            mirror=mirror, ns=instance.namespace, write=core.enqueue_write
        )
        with patch.object(
            StartupScanService,
            "_scan_all_mounts",
            new=AsyncMock(return_value=checkpoints),
        ):
            svc = StartupScanService(
                drive_api=MagicMock(),
                mirror=mirror,
                store=store,
                syno_paths=SynologyPathService(
                    ids=IdentityIds(),
                    registry=MountRegistry(mounts=mounts_of(mounts), root_ids={}),
                    store=store,
                ),
                enrichment=core.enrichment,
                scans=core.scans,
            )
            await svc.run_initial_scan()
        await core.writes_landed()

    async def test_first_run_writes_per_mount_state(self) -> None:
        """On first run (all zeros), scan results are persisted via set_mount_state."""
        # given
        async with backend_harness() as server:
            mirror = server.mirror
            mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}
            instance = await server.add_instance(
                "synology", SYNOLOGY_SCHEMA, {k: p.path for k, p in mounts.items()}
            )

            # when
            await self._run_initial_sync(instance, mirror, mounts, {"a": 500, "b": 300})

            # then
            result = await SynologyStoreReader(
                mirror=mirror, ns=mirror.namespace("synology")
            ).get_mount_max_ids(mounts)
            self.assertEqual(result, {"a": 500, "b": 300})

    async def test_resume_advances_max_id(self) -> None:
        """On resume, scan results that advance the max_id are persisted."""
        # given
        async with backend_harness() as server:
            mirror = server.mirror
            mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}
            instance = await server.add_instance(
                "synology", SYNOLOGY_SCHEMA, {k: p.path for k, p in mounts.items()}
            )
            # Seed existing per-mount state
            await SynologyStore(
                mirror=mirror,
                ns=mirror.namespace("synology"),
                write=mirror.write,
            ).set_mount_state("a", "/vol/a", 100)
            await SynologyStore(
                mirror=mirror,
                ns=mirror.namespace("synology"),
                write=mirror.write,
            ).set_mount_state("b", "/vol/b", 80)

            # when
            await self._run_initial_sync(instance, mirror, mounts, {"a": 200, "b": 150})

            # then
            result = await SynologyStoreReader(
                mirror=mirror, ns=mirror.namespace("synology")
            ).get_mount_max_ids(mounts)
            self.assertEqual(result, {"a": 200, "b": 150})

    async def test_no_advance_skips_write(self) -> None:
        """When scan returns the same max_id, set_mount_state is not called."""
        # given
        async with backend_harness() as server:
            mirror = server.mirror
            mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}
            instance = await server.add_instance(
                "synology", SYNOLOGY_SCHEMA, {k: p.path for k, p in mounts.items()}
            )
            await SynologyStore(
                mirror=mirror,
                ns=mirror.namespace("synology"),
                write=mirror.write,
            ).set_mount_state("a", "/vol/a", 100)
            await SynologyStore(
                mirror=mirror,
                ns=mirror.namespace("synology"),
                write=mirror.write,
            ).set_mount_state("b", "/vol/b", 80)

            # when
            # Scan returns identical values — no advance
            await self._run_initial_sync(instance, mirror, mounts, {"a": 100, "b": 80})

            # then
            result = await SynologyStoreReader(
                mirror=mirror, ns=mirror.namespace("synology")
            ).get_mount_max_ids(mounts)
            self.assertEqual(result, {"a": 100, "b": 80})

    async def test_initial_sync_uses_pre_scan_checkpoint(self) -> None:
        """Initial sync saves the watermark the scan returned.

        Changes that occur in already-visited folders mid-BFS get sync_ids
        lower than the highest sync_id seen but higher than pre_scan_max_id.  Saving
        the checkpoint lets the first incremental scan cover that window.
        """
        # given
        async with backend_harness() as server:
            mirror = server.mirror
            mounts = {"a": SynologyPath("/vol/a")}
            instance = await server.add_instance(
                "synology", SYNOLOGY_SCHEMA, {k: p.path for k, p in mounts.items()}
            )

            # when
            # The watermark read before traversal; the next scan re-covers
            # anything that changed in folders already visited.
            await self._run_initial_sync(
                instance,
                mirror,
                mounts,
                {"a": 300},
            )

            # then
            result = await SynologyStoreReader(
                mirror=mirror, ns=mirror.namespace("synology")
            ).get_mount_max_ids(mounts)
            self.assertEqual(result, {"a": 300})


class TestMountPass(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mirror: Any = MagicMock()
        self.mirror.get_children = AsyncMock(return_value=[MagicMock()])
        self.drive_api: Any = MagicMock()
        self.drive_api.list_folder = AsyncMock(return_value=([], 1))
        self.paths: Any = MagicMock()
        self.paths.mounts = {"a": MagicMock(id=NodeId("_a"))}
        self.paths.permanent_link = AsyncMock(return_value="link")
        self.scan = _MountPass(
            drive_api=self.drive_api,
            mirror=self.mirror,
            syno_paths=self.paths,
            last_max_ids={"a": 100},
        )

    def old_folder(self, *, force: bool = False) -> _Folder:
        return _Folder(NodeId("d"), "a", max_id=50, force=force)

    async def test_unchanged_folder_is_skipped(self) -> None:
        # when, then
        self.assertTrue(await self.scan.skip(self.old_folder()))

    async def test_failed_count_check_still_skips(self) -> None:
        # given
        self.drive_api.list_folder.side_effect = SynologyNetworkError("NAS unavailable")
        # when, then
        self.assertTrue(await self.scan.skip(self.old_folder()))

    async def test_failed_count_check_from_a_bug_fails(self) -> None:
        # given
        self.drive_api.list_folder.side_effect = KeyError("total")
        # when, then
        with self.assertRaises(KeyError):
            await self.scan.skip(self.old_folder())

    async def test_new_folder_is_entered_whatever_its_max_id(self) -> None:
        # when
        skipped = await self.scan.skip(self.old_folder(force=True))
        # then
        self.assertFalse(skipped)
        self.drive_api.list_folder.assert_not_awaited()

    async def test_checkpoint_is_the_watermark_read_before_the_scan(self) -> None:
        # given: the mount reports a watermark below a later change
        item = {"type": "file", "sync_id": 200, "max_id": 150}
        observation = MagicMock()
        observation.record.id = NodeId("f")
        self.paths.list_for_pass = AsyncMock(return_value=[item])
        self.paths.convert = AsyncMock(return_value=[(item, observation)])
        root = _Folder(NodeId("_a"), "a", is_mount=True)
        # when
        await self.scan.list_folder(root)
        await self.scan.root_finished(root, True)
        # then
        self.assertEqual(self.scan.pre_scan_max_ids["a"], 150)

    async def test_only_nodes_changed_since_the_checkpoint_are_written(self) -> None:
        # given: both are mirrored, and one changed after the checkpoint
        changed = {"type": "file", "sync_id": 150}
        unchanged = {"type": "file", "sync_id": 50}
        changed_fact = MagicMock()
        changed_fact.record.id = NodeId("changed")
        unchanged_fact = MagicMock()
        unchanged_fact.record.id = NodeId("unchanged")
        self.mirror.get_children = AsyncMock(
            return_value=[
                MagicMock(id=NodeId("changed")),
                MagicMock(id=NodeId("unchanged")),
            ]
        )
        self.paths.list_for_pass = AsyncMock(return_value=[changed, unchanged])
        self.paths.convert = AsyncMock(
            return_value=[(changed, changed_fact), (unchanged, unchanged_fact)]
        )
        # when
        listing = await self.scan.list_folder(_Folder(NodeId("d"), "a"))
        # then
        self.assertEqual(listing.changed, [changed_fact])
        self.assertEqual(listing.seen, [NodeId("changed"), NodeId("unchanged")])

    def test_mounts_are_scanned_one_at_a_time(self) -> None:
        # when, then
        self.assertFalse(self.scan.concurrent_roots)


class TestListForPass(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=make_mounts({"a": "/a"}), root_ids={}),
            store=MagicMock(),
        )
        self.drive_api: Any = MagicMock()

    async def test_nas_failure_is_an_unavailable_listing(self) -> None:
        # given
        self.drive_api.list_folder = AsyncMock(
            side_effect=SynologyNetworkError("NAS unavailable")
        )
        # when, then
        with self.assertRaisesRegex(ListingUnavailableError, "NAS unavailable"):
            await self.paths.list_for_pass(self.drive_api, NodeId("_a"))

    async def test_other_failures_pass_through(self) -> None:
        # given
        self.drive_api.list_folder = AsyncMock(side_effect=KeyError("total"))
        # when, then
        with self.assertRaises(KeyError):
            await self.paths.list_for_pass(self.drive_api, NodeId("_a"))


class TestStartupOrder(IsolatedAsyncioTestCase):
    async def test_failed_media_is_retried_before_checkpoints_are_queued(self) -> None:
        # given
        log: list[str] = []
        store: Any = MagicMock()
        store.get_mount_max_ids = AsyncMock(return_value={"a": 10})
        store.set_mount_state = AsyncMock(
            side_effect=lambda *_: log.append("checkpoint")
        )
        enrichment: Any = MagicMock()
        enrichment.retry_failures = AsyncMock(side_effect=lambda _: log.append("retry"))
        paths: Any = MagicMock()
        paths.mounts = {"a": MagicMock()}
        paths.paths = {"a": SynologyPath("/vol/a")}
        service = StartupScanService(
            drive_api=MagicMock(),
            mirror=MagicMock(),
            store=store,
            syno_paths=paths,
            enrichment=enrichment,
            scans=MagicMock(),
        )
        # when
        with patch.object(
            service,
            "_scan_all_mounts",
            new=AsyncMock(return_value={"a": 20}),
        ):
            await service.run_initial_scan()
        # then
        self.assertEqual(log, ["retry", "checkpoint"])

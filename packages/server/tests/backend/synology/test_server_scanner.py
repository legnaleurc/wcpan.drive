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
from wcpan.drive.server._backend import Mounts
from wcpan.drive.server._lib.write_queue import (
    WriteQueue,
    create_write_queue,
)
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.sync.scan import ScanRunner
from wcpan.drive.server._services.sync.service import NodeSyncService
from wcpan.drive.server.backend.synology._mounts import MountRegistry
from wcpan.drive.server.backend.synology._paths import SynologyPathService
from wcpan.drive.server.backend.synology._scan import (
    StartupScanService,
    _Folder,
    _MountPass,
)
from wcpan.drive.server.backend.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.synology.types import (
    SynologyClient,
    SynologyPath,
)

from ._lib import DecoyTestCase, IdentityIds, mounts_of


class TestScannerWriteBack(DecoyTestCase):
    async def _drain_writes(self, q: WriteQueue) -> None:
        try:
            while True:
                job = await q.get()
                try:
                    await job()
                finally:
                    q.task_done()
        except asyncio.CancelledError:
            raise

    async def _run_initial_sync(
        self,
        mirror: MirrorStore,
        mounts: dict[str, SynologyPath],
        scan_result: dict[str, int],
        pre_scan_result: dict[str, int] | None = None,
    ) -> None:
        if pre_scan_result is None:
            pre_scan_result = scan_result
        q = create_write_queue()
        with ThreadPoolExecutor(2) as pool:
            off_main = OffMainService(pool=pool)
            cs = NodeSyncService(
                mirror=mirror,
                write_queue=q,
                probing=MediaProbing(runner=ProbeRunner()),
                metadata_queue=asyncio.Queue(),
            ).for_backend("synology")
            drain = asyncio.create_task(self._drain_writes(q))
            try:
                with patch.object(
                    StartupScanService,
                    "_scan_all_mounts",
                    new=AsyncMock(return_value=(scan_result, pre_scan_result)),
                ):
                    svc = StartupScanService(
                        drive_api=self.decoy.mock(cls=SynologyClient),
                        mirror=mirror,
                        store=SynologyStore(
                            mirror=mirror, ns=mirror.namespace("synology")
                        ),
                        syno_paths=SynologyPathService(
                            ids=IdentityIds(),
                            registry=MountRegistry(
                                mounts=mounts_of(mounts), root_ids={}
                            ),
                            store=SynologyStore(
                                mirror=mirror, ns=mirror.namespace("synology")
                            ),
                        ),
                        node_sync=cs,
                        scans=ScanRunner(mirror=mirror, node_sync=cs),
                    )
                    await svc.run_initial_scan()
            finally:
                await q.join()
                drain.cancel()
                with suppress(asyncio.CancelledError):
                    await drain

    async def test_first_run_writes_per_mount_state(self) -> None:
        """On first run (all zeros), scan results are persisted via set_mount_state."""
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = MirrorStore(
                    db_path,
                    off_main=OffMainService(pool=pool),
                    schemas={"synology": SYNOLOGY_SCHEMA},
                )
                await mirror.ensure_schema()
                mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}

                # when
                await self._run_initial_sync(mirror, mounts, {"a": 500, "b": 300})

                # then
                result = await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).get_mount_max_ids(mounts)
                self.assertEqual(result, {"a": 500, "b": 300})
        finally:
            os.unlink(db_path)

    async def test_resume_advances_max_id(self) -> None:
        """On resume, scan results that advance the max_id are persisted."""
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = MirrorStore(
                    db_path,
                    off_main=OffMainService(pool=pool),
                    schemas={"synology": SYNOLOGY_SCHEMA},
                )
                await mirror.ensure_schema()
                mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}
                # Seed existing per-mount state
                await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).set_mount_state("a", "/vol/a", 100)
                await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).set_mount_state("b", "/vol/b", 80)

                # when
                await self._run_initial_sync(mirror, mounts, {"a": 200, "b": 150})

                # then
                result = await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).get_mount_max_ids(mounts)
                self.assertEqual(result, {"a": 200, "b": 150})
        finally:
            os.unlink(db_path)

    async def test_no_advance_skips_write(self) -> None:
        """When scan returns the same max_id, set_mount_state is not called."""
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = MirrorStore(
                    db_path,
                    off_main=OffMainService(pool=pool),
                    schemas={"synology": SYNOLOGY_SCHEMA},
                )
                await mirror.ensure_schema()
                mounts = {"a": SynologyPath("/vol/a"), "b": SynologyPath("/vol/b")}
                await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).set_mount_state("a", "/vol/a", 100)
                await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).set_mount_state("b", "/vol/b", 80)

                # when
                # Scan returns identical values — no advance
                await self._run_initial_sync(mirror, mounts, {"a": 100, "b": 80})

                # then
                result = await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).get_mount_max_ids(mounts)
                self.assertEqual(result, {"a": 100, "b": 80})
        finally:
            os.unlink(db_path)

    async def test_initial_sync_uses_pre_scan_checkpoint(self) -> None:
        """Initial sync saves the pre-scan checkpoint, not per_mount_highest.

        Changes that occur in already-visited folders mid-BFS get sync_ids
        lower than per_mount_highest but higher than pre_scan_max_id.  Saving
        the checkpoint lets the first incremental scan cover that window.
        """
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = MirrorStore(
                    db_path,
                    off_main=OffMainService(pool=pool),
                    schemas={"synology": SYNOLOGY_SCHEMA},
                )
                await mirror.ensure_schema()
                mounts = {"a": SynologyPath("/vol/a")}

                # when
                # per_mount_highest=500 (saw a late folder), pre_scan=300 (snapshot
                # taken before BFS started); the DB must record 300 so that the
                # incremental scan re-covers sync_ids 301-500 and catches any
                # mid-BFS changes in already-visited folders.
                await self._run_initial_sync(
                    mirror,
                    mounts,
                    scan_result={"a": 500},
                    pre_scan_result={"a": 300},
                )

                # then
                result = await SynologyStore(
                    mirror=mirror, ns=mirror.namespace("synology")
                ).get_mount_max_ids(mounts)
                self.assertEqual(result, {"a": 300})
        finally:
            os.unlink(db_path)


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
        self.drive_api.list_folder.side_effect = OSError("NAS unavailable")
        # when, then
        self.assertTrue(await self.scan.skip(self.old_folder()))

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
        self.paths.list_children = AsyncMock(return_value=[item])
        self.paths.convert = AsyncMock(return_value=[(item, observation)])
        root = _Folder(NodeId("_a"), "a", is_mount=True)
        # when
        await self.scan.list_folder(root)
        await self.scan.root_finished(root, True)
        # then
        self.assertEqual(self.scan.pre_scan_max_ids["a"], 150)
        self.assertEqual(self.scan.per_mount_highest["a"], 200)

    def test_mounts_are_scanned_one_at_a_time(self) -> None:
        # when, then
        self.assertFalse(self.scan.concurrent_roots)

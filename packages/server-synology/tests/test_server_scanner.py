"""Tests for scanner write-back of per-mount state to database."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from unittest.mock import AsyncMock, patch

from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology.mounts import MountRegistry
from wcpan.drive.server.synology.paths import SynologyPathService
from wcpan.drive.server.synology.scan import StartupScanService
from wcpan.drive.server.synology.store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.types import WriteQueue
from wcpan.drive.server.workers import create_write_queue
from wcpan.synology import SynologyClient, SynologyPath

from ._lib import DecoyTestCase


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
                off_main=off_main,
                mounts={},
                metadata_queue=asyncio.Queue(),
            )
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
                        store=SynologyStore(mirror=mirror),
                        syno_paths=SynologyPathService(
                            registry=MountRegistry(mounts=mounts, root_ids={}),
                            store=SynologyStore(mirror=mirror),
                        ),
                        node_sync=cs,
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
                result = await SynologyStore(mirror=mirror).get_mount_max_ids(mounts)
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
                await SynologyStore(mirror=mirror).set_mount_state("a", "/vol/a", 100)
                await SynologyStore(mirror=mirror).set_mount_state("b", "/vol/b", 80)

                # when
                await self._run_initial_sync(mirror, mounts, {"a": 200, "b": 150})

                # then
                result = await SynologyStore(mirror=mirror).get_mount_max_ids(mounts)
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
                await SynologyStore(mirror=mirror).set_mount_state("a", "/vol/a", 100)
                await SynologyStore(mirror=mirror).set_mount_state("b", "/vol/b", 80)

                # when
                # Scan returns identical values — no advance
                await self._run_initial_sync(mirror, mounts, {"a": 100, "b": 80})

                # then
                result = await SynologyStore(mirror=mirror).get_mount_max_ids(mounts)
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
                result = await SynologyStore(mirror=mirror).get_mount_max_ids(mounts)
                self.assertEqual(result, {"a": 300})
        finally:
            os.unlink(db_path)

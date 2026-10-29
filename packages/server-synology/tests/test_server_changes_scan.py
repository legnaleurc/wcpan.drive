"""Tests for deferred deletion in ``StartupScanService._scan_all_mounts``."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from unittest.mock import patch

import wcpan.drive.server.synology.scan as startup_scan_mod
from decoy import matchers
from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.lib.mounts import (
    SERVER_ROOT_ID,
    mount_id,
    mount_name,
)
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology.convert import SynologyLocator
from wcpan.drive.server.synology.mounts import MountRegistry
from wcpan.drive.server.synology.paths import SynologyPathService
from wcpan.drive.server.synology.scan import StartupScanService
from wcpan.drive.server.synology.store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.types import (
    NodeObservation,
    WriteQueue,
)
from wcpan.drive.server.workers import create_write_queue, metadata_worker
from wcpan.synology import SynologyClient, SynologyFileId

from ._lib import DecoyTestCase


def _node(
    node_id: str,
    name: str,
    parent_id: str | None,
    *,
    is_directory: bool = False,
) -> NodeRecord:
    t = 1_704_067_200
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=t,
        modified_time=t,
        changed_time=t,
        mime_type=FOLDER_MIME_TYPE if is_directory else "text/plain",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _syno_item(
    file_id: str,
    name: str,
    *,
    is_dir: bool = False,
    sync_id: int = 200,
    max_id: int | None = None,
) -> dict:
    mid = max_id if max_id is not None else sync_id
    return {
        "file_id": file_id,
        "permanent_link": file_id,
        "parent_id": "",
        "name": name,
        "type": "dir" if is_dir else "file",
        "content_type": "dir" if is_dir else "file",
        "size": 0,
        "created_time": 0,
        "modified_time": 0,
        "change_time": 0,
        "sync_id": sync_id,
        "max_id": mid,
    }


async def _noop_enrich_node_sync(_self: object, record: NodeRecord) -> NodeRecord:
    return record


class TestDeferredScan(DecoyTestCase):
    async def _drive_api(self, count: int = 0) -> SynologyClient:
        """A drive API whose folder counts are *count* and whose nodes are gone."""
        drive_api = self.decoy.mock(cls=SynologyClient)
        self.decoy.when(
            await drive_api.list_folder(
                matchers.Anything(),
                offset=matchers.Anything(),
                limit=matchers.Anything(),
            )
        ).then_return(([], count))
        return drive_api

    def _push_incremental_scan_mocks(
        self,
        stack: ExitStack,
        *,
        by_path,
        list_all=None,
        mute_tree_log_exception: bool = False,
    ) -> None:
        async def api_children(_self_svc: object, _net: object, parent_id: str) -> list:
            mkey = mount_name(parent_id)
            if mkey is not None:
                mounts = _self_svc.mounts  # type: ignore[union-attr]
                return await by_path(_net, mounts[mkey])
            if list_all is None:
                raise AssertionError(f"unexpected deep listing for {parent_id!r}")
            return await list_all(_net, parent_id)

        stack.enter_context(
            patch.object(
                SynologyPathService,
                "list_children",
                new=api_children,
            )
        )
        stack.enter_context(
            patch.object(NodeSyncService, "enrich", new=_noop_enrich_node_sync)
        )
        if mute_tree_log_exception:
            stack.enter_context(patch.object(startup_scan_mod._L, "exception"))

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

    async def test_cross_mount_reparent_no_remove_change_for_file_id(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node(mount_id("b"), "b", SERVER_ROOT_ID, is_directory=True),
                        _node("x1", "moved.txt", mount_id("a")),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return []
                    if path == "/vol/b":
                        return [_syno_item("x1", "moved.txt", is_dir=False)]
                    return []

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(stack, by_path=by_path)
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a", "b": "/vol/b"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 50, "b": 50})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                row = await mirror.get_node_by_id("x1")
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row.parent_id, mount_id("b"))

                changes, _, _ = await mirror.get_changes_since(0, max_size=500)
                x1_removes = [c for c in changes if c[0] == "x1" and c[1] is True]
                self.assertEqual(x1_removes, [])
        finally:
            os.unlink(db_path)

    async def test_max_id_prune_preserves_db_subtree(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node("dirF", "F", mount_id("a"), is_directory=True),
                        _node("childC", "c.txt", "dirF"),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item("dirF", "F", is_dir=True, sync_id=10, max_id=10)
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    raise AssertionError(f"unexpected list of {folder_id}")

                # Unlisted nodes are confirmed missing before removal.
                drive_api = await self._drive_api(count=1)
                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack,
                            by_path=by_path,
                            list_all=list_all,
                        )
                        svc = StartupScanService(
                            drive_api=drive_api,
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 100})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("dirF"))
                self.assertIsNotNone(await mirror.get_node_by_id("childC"))
        finally:
            os.unlink(db_path)

    async def test_initial_scan_enters_folder_with_zero_max_id(self) -> None:
        """Full initial scan (last_max_id=0) must enter folders whose max_id is 0."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item("dirF", "F", is_dir=True, sync_id=0, max_id=0)
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    if folder_id == "dirF":
                        return [_syno_item("childC", "c.txt", sync_id=0)]
                    return []

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack, by_path=by_path, list_all=list_all
                        )
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 0})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("dirF"))
                self.assertIsNotNone(await mirror.get_node_by_id("childC"))
        finally:
            os.unlink(db_path)

    async def test_new_folder_with_stale_max_id_is_force_scanned(self) -> None:
        """A folder absent from DB must be entered even when max_id <= last_max_id."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item("dirF", "F", is_dir=True, sync_id=40, max_id=50)
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    if folder_id == "dirF":
                        return [_syno_item("childC", "c.txt", sync_id=50)]
                    return []

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack, by_path=by_path, list_all=list_all
                        )
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 100})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("dirF"))
                self.assertIsNotNone(await mirror.get_node_by_id("childC"))
        finally:
            os.unlink(db_path)

    async def test_db_folder_with_no_children_is_force_scanned(self) -> None:
        """A folder already in DB but with no DB children must be entered even
        when max_id <= last_max_id (its contents were never successfully scanned)."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node("dirF", "F", mount_id("a"), is_directory=True),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item("dirF", "F", is_dir=True, sync_id=40, max_id=50)
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    if folder_id == "dirF":
                        return [_syno_item("childC", "c.txt", sync_id=50)]
                    return []

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack, by_path=by_path, list_all=list_all
                        )
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 100})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("dirF"))
                self.assertIsNotNone(await mirror.get_node_by_id("childC"))
        finally:
            os.unlink(db_path)

    async def test_list_failure_preserves_db_subtree(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node("dirF", "F", mount_id("a"), is_directory=True),
                        _node("childC", "c.txt", "dirF"),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item(
                                "dirF", "F", is_dir=True, sync_id=200, max_id=200
                            )
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    if folder_id == "dirF":
                        raise OSError("boom")
                    return []

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack,
                            by_path=by_path,
                            list_all=list_all,
                            mute_tree_log_exception=True,
                        )
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 50})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("dirF"))
                self.assertIsNotNone(await mirror.get_node_by_id("childC"))
        finally:
            os.unlink(db_path)

    async def test_max_id_prune_detects_deletion_via_count_mismatch(self) -> None:
        """max_id unchanged but a child deleted: count check triggers a full scan."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node(
                            "perm:dirF",
                            "F",
                            mount_id("a"),
                            is_directory=True,
                        ),
                        _node("child1", "a.txt", "perm:dirF"),
                        _node("child2", "b.txt", "perm:dirF"),
                    ]
                )
                await mirror.apply_observations(
                    [
                        NodeObservation(
                            record=_node(
                                "perm:dirF", "F", mount_id("a"), is_directory=True
                            ),
                            locator=SynologyLocator("dirF"),
                        )
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            {
                                **_syno_item(
                                    "dirF", "F", is_dir=True, sync_id=10, max_id=10
                                ),
                                "permanent_link": "perm:dirF",
                            }
                        ]
                    return []

                async def list_all(_net: object, folder_id: str) -> list:
                    if folder_id == "perm:dirF":
                        return [_syno_item("child1", "a.txt")]
                    return []

                # Unlisted nodes are confirmed missing before removal.
                drive_api = await self._drive_api()
                self.decoy.when(
                    await drive_api.list_folder(
                        SynologyFileId("dirF"), offset=0, limit=1
                    )
                ).then_return(([], 1))
                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack,
                            by_path=by_path,
                            list_all=list_all,
                        )
                        svc = StartupScanService(
                            drive_api=drive_api,
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 100})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertIsNotNone(await mirror.get_node_by_id("child1"))
                self.assertIsNone(await mirror.get_node_by_id("child2"))
        finally:
            os.unlink(db_path)

    async def test_failed_mount_does_not_advance_its_max_id(self) -> None:
        """A mount whose top-level list fails returns its input last_max_id unchanged."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node(mount_id("b"), "b", SERVER_ROOT_ID, is_directory=True),
                    ]
                )

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        raise OSError("mount a unavailable")
                    return [_syno_item("f1", "file.txt", sync_id=200)]

                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack,
                            by_path=by_path,
                            mute_tree_log_exception=True,
                        )
                        svc = StartupScanService(
                            drive_api=self.decoy.mock(cls=SynologyClient),
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a", "b": "/vol/b"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        result = await svc._scan_all_mounts({"a": 100, "b": 100})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                per_mount_highest, _ = result
                self.assertEqual(per_mount_highest["a"], 100)
                self.assertEqual(per_mount_highest["b"], 200)
        finally:
            os.unlink(db_path)

    async def test_per_mount_pruning_thresholds_are_independent(self) -> None:
        """Each mount uses its own last_max_id threshold for BFS pruning."""
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor(2) as pool:
                off_main = OffMainService(pool=pool)
                mirror = MirrorStore(
                    db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
                )
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [
                        _node(SERVER_ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("a"), "a", SERVER_ROOT_ID, is_directory=True),
                        _node(mount_id("b"), "b", SERVER_ROOT_ID, is_directory=True),
                        _node("dirX_a", "X", mount_id("a"), is_directory=True),
                        _node("child_a", "c.txt", "dirX_a"),
                        _node("dirX_b", "X", mount_id("b"), is_directory=True),
                        _node("child_b", "c.txt", "dirX_b"),
                    ]
                )

                entered: list[str] = []

                async def by_path(_net: object, path: str) -> list:
                    if path == "/vol/a":
                        return [
                            _syno_item(
                                "dirX_a", "X", is_dir=True, sync_id=50, max_id=80
                            )
                        ]
                    return [
                        _syno_item("dirX_b", "X", is_dir=True, sync_id=50, max_id=80)
                    ]

                async def list_all(_net: object, folder_id: str) -> list:
                    entered.append(folder_id)
                    return []

                # Unlisted nodes are confirmed missing before removal.
                drive_api = await self._drive_api(count=1)
                q = create_write_queue()
                mq: asyncio.Queue = asyncio.Queue()
                cs = NodeSyncService(
                    mirror=mirror,
                    write_queue=q,
                    off_main=off_main,
                    mounts={},
                    metadata_queue=mq,
                )
                drain = asyncio.create_task(self._drain_writes(q))
                meta_drain = asyncio.create_task(
                    metadata_worker(mq, cs.process_metadata_item)
                )
                try:
                    with ExitStack() as stack:
                        self._push_incremental_scan_mocks(
                            stack,
                            by_path=by_path,
                            list_all=list_all,
                        )
                        svc = StartupScanService(
                            drive_api=drive_api,
                            mirror=mirror,
                            store=SynologyStore(mirror=mirror),
                            syno_paths=SynologyPathService(
                                registry=MountRegistry(
                                    mounts={"a": "/vol/a", "b": "/vol/b"}, root_ids={}
                                ),
                                store=SynologyStore(mirror=mirror),
                            ),
                            node_sync=cs,
                        )
                        await svc._scan_all_mounts({"a": 100, "b": 50})
                finally:
                    await mq.join()
                    await q.join()
                    meta_drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await meta_drain
                    drain.cancel()
                    with suppress(asyncio.CancelledError):
                        await drain

                self.assertNotIn("dirX_a", entered)
                self.assertIn("dirX_b", entered)
        finally:
            os.unlink(db_path)

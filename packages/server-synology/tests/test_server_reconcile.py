"""Tests for API backfill / subtree reconcile."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import PurePosixPath
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import VirtualPathService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology._backfill import BackfillService
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)
from wcpan.synology import SynologyClient, SynologyPath

from ._lib import ROOT_ID, DecoyTestCase, IdentityIds, make_mounts, mount_id


_TS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


async def _reconcile_with_worker(
    mirror: MirrorStore,
    mounts: dict[str, str],
    drive_api: SynologyClient,
    root_id: str,
    *,
    dry_run: bool,
    list_children_side_effect: object,
) -> dict[str, int]:
    q = create_write_queue()
    mq = create_metadata_queue()
    with ThreadPoolExecutor(2) as pool:
        off_main = OffMainService(pool=pool)
        cs = NodeSyncService(
            mirror=mirror,
            write_queue=q,
            off_main=off_main,
            mounts={},
            metadata_queue=mq,
        ).for_backend("synology")
        syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=make_mounts(mounts), root_ids={}),
            store=SynologyStore(mirror=mirror, ns=mirror.namespace("synology")),
        )
        bf = BackfillService(
            drive_api=drive_api,
            mirror=mirror,
            store=SynologyStore(mirror=mirror, ns=mirror.namespace("synology")),
            syno_paths=syno_paths,
            node_sync=cs,
        )
        write_task = asyncio.create_task(write_worker(q, noop_after_write))
        meta_task = asyncio.create_task(metadata_worker(mq, cs.process_metadata_item))
        try:
            with patch.object(
                syno_paths,
                "list_children",
                side_effect=list_children_side_effect,
            ):
                stats = await bf._reconcile_subtree(root_id, dry_run=dry_run)
            await mq.join()
            await q.join()
            return stats
        finally:
            meta_task.cancel()
            with suppress(asyncio.CancelledError):
                await meta_task
            write_task.cancel()
            with suppress(asyncio.CancelledError):
                await write_task


def _node(
    node_id: str,
    name: str,
    parent_id: str | None,
    *,
    is_directory: bool = False,
    size: int = 0,
    width: int = 0,
) -> NodeRecord:
    t = _TS
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
        size=size,
        is_image=False,
        is_video=False,
        width=width,
        height=0,
        ms_duration=0,
    )


def _syno_item(
    file_id: str,
    name: str,
    *,
    is_dir: bool = False,
    size: int = 0,
    modified_time: int = _TS,
    created_time: int = _TS,
) -> dict:
    return {
        "file_id": file_id,
        "permanent_link": file_id,
        "parent_id": "",
        "name": name,
        "type": "dir" if is_dir else "file",
        "content_type": "dir" if is_dir else "file",
        "size": size,
        "created_time": created_time,
        "modified_time": modified_time,
        "change_time": 0,
        "sync_id": 1,
    }


class TestVirtualPathToDirectoryNodeId(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"synology": SYNOLOGY_SCHEMA},
        )
        await mirror.ensure_schema()
        await mirror.bulk_upsert_nodes(
            [
                _node(ROOT_ID, "", None, is_directory=True),
                _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
            ]
        )
        self.svc = VirtualPathService(mirror=mirror)

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    async def test_root_slash(self) -> None:
        # when / then
        self.assertEqual(
            await self.svc.resolve_to_directory_node_id("/"),
            ROOT_ID,
        )

    async def test_root_empty(self) -> None:
        # when / then
        self.assertEqual(
            await self.svc.resolve_to_directory_node_id("  "),
            ROOT_ID,
        )

    async def test_mount_only(self) -> None:
        # when / then
        self.assertEqual(
            await self.svc.resolve_to_directory_node_id("/docs"),
            mount_id("docs"),
        )

    async def test_nested(self) -> None:
        # when / then
        self.assertEqual(
            await self.svc.resolve_to_directory_node_id("/docs/Projects"),
            "dir-1",
        )

    async def test_rejects_dotdot(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            await self.svc.resolve_to_directory_node_id("/docs/../x")

    async def test_missing_segment_raises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            await self.svc.resolve_to_directory_node_id("/docs/Nope")


class TestReconcileSubtree(DecoyTestCase):
    async def test_updates_mismatch_and_preserves_width(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                        _node("f-1", "x.txt", "dir-1", size=0, width=42),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-1", "x.txt", size=999)]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=False,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["checked"], 1)
                self.assertEqual(stats["updated"], 1)
                self.assertEqual(stats["added"], 0)
                updated = await mirror.get_node_by_id("f-1")
                assert updated is not None
                self.assertEqual(updated.size, 999)
                self.assertEqual(updated.width, 42)
        finally:
            os.unlink(db_path)

    async def test_dry_run_no_write(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                        _node("f-1", "x.txt", "dir-1", size=0),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-1", "x.txt", size=500)]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=True,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["updated"], 1)
                updated = await mirror.get_node_by_id("f-1")
                assert updated is not None
                self.assertEqual(updated.size, 0)
        finally:
            os.unlink(db_path)

    async def test_adds_missing_node(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-new", "new.txt", size=100)]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=False,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["checked"], 1)
                self.assertEqual(stats["added"], 1)
                self.assertEqual(stats["updated"], 0)
                added = await mirror.get_node_by_id("f-new")
                self.assertIsNotNone(added)
                assert added is not None
                self.assertEqual(added.name, "new.txt")
                self.assertEqual(added.parent_id, "dir-1")
        finally:
            os.unlink(db_path)

    async def test_server_root_queues_mounts(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)
                listed: list[str] = []

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    listed.append(parent_id)
                    if parent_id == mount_id("docs"):
                        return [_syno_item("f-1", "readme.txt")]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    ROOT_ID,
                    dry_run=False,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertIn(mount_id("docs"), listed)
                self.assertEqual(stats["checked"], 1)
                self.assertEqual(stats["added"], 1)
        finally:
            os.unlink(db_path)

    async def test_adds_missing_node_dry_run(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-new", "new.txt")]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=True,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["added"], 1)
                self.assertIsNone(await mirror.get_node_by_id("f-new"))
        finally:
            os.unlink(db_path)

    async def test_removes_node_absent_from_api(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                        _node("f-1", "keep.txt", "dir-1"),
                        _node("f-2", "deleted.txt", "dir-1"),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-1", "keep.txt")]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=False,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["checked"], 1)
                self.assertEqual(stats["removed"], 1)
                self.assertIsNotNone(await mirror.get_node_by_id("f-1"))
                self.assertIsNone(await mirror.get_node_by_id("f-2"))
        finally:
            os.unlink(db_path)

    async def test_removes_node_absent_from_api_dry_run(self) -> None:
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
                await mirror.bulk_upsert_nodes(
                    [
                        _node(ROOT_ID, "", None, is_directory=True),
                        _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
                        _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
                        _node("f-1", "keep.txt", "dir-1"),
                        _node("f-2", "deleted.txt", "dir-1"),
                    ]
                )
                mounts = {"docs": "/volume1/docs"}
                drive_api = self.decoy.mock(cls=SynologyClient)

                async def _list_children(_nw: object, parent_id: str) -> list[dict]:
                    if parent_id == "dir-1":
                        return [_syno_item("f-1", "keep.txt")]
                    return []

                # when
                stats = await _reconcile_with_worker(
                    mirror,
                    mounts,
                    drive_api,
                    "dir-1",
                    dry_run=True,
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertEqual(stats["removed"], 1)
                self.assertIsNotNone(await mirror.get_node_by_id("f-2"))
        finally:
            os.unlink(db_path)

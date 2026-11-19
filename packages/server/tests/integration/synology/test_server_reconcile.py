"""The Synology backfill comparison against a real mirror."""

import asyncio
import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from decoy import matchers
from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._lib.write_queue import (
    create_write_queue,
    write_worker,
)
from wcpan.drive.server._services.ids import ProvisionalNodeIds
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.maintenance import (
    checkpoint_database,
)
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.paths import VirtualPathService
from wcpan.drive.server._services.sync.metadata import (
    create_metadata_queue,
    metadata_worker,
)
from wcpan.drive.server._services.sync.scan import compare_pass
from wcpan.drive.server._services.sync.scanner import ComparisonReport
from wcpan.drive.server._services.sync.writer import MirrorWriter
from wcpan.drive.server._types import NodeObservation, ReadSeq
from wcpan.drive.server.backend.synology._convert import SynologyLocator
from wcpan.drive.server.backend.synology._mounts import MountRegistry
from wcpan.drive.server.backend.synology._paths import SynologyPathService
from wcpan.drive.server.backend.synology._store import (
    SYNOLOGY_SCHEMA,
    SynologyStore,
    SynologyStoreReader,
)
from wcpan.drive.sqlite import read_only_dsn
from wcpan.synology.exceptions import SynologyNotFoundError
from wcpan.synology.types import (
    SynologyClient,
    SynologyPath,
)

from ...backend.synology._lib import (
    ROOT_ID,
    DecoyTestCase,
    IdentityIds,
    make_mounts,
    mount_id,
)
from ._lib import backfill as run_backfill


_TS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


async def _reconcile_with_worker(
    mirror: MirrorStore,
    mounts: dict[str, str],
    drive_api: SynologyClient,
    root_id: str,
    *,
    list_children_side_effect: object,
) -> ComparisonReport:
    q = create_write_queue()
    mq = create_metadata_queue()
    cs = MirrorWriter(
        mirror=mirror,
        write_queue=q,
        probing=MediaProbing(runner=ProbeRunner()),
        metadata_queue=mq,
    ).for_backend("synology")
    syno_paths = SynologyPathService(
        ids=IdentityIds(),
        registry=MountRegistry(mounts=make_mounts(mounts), root_ids={}),
        store=SynologyStoreReader(mirror=mirror, ns=mirror.namespace("synology")),
    )
    bf = partial(
        run_backfill,
        drive_api=drive_api,
        mirror=mirror,
        store=SynologyStoreReader(mirror=mirror, ns=mirror.namespace("synology")),
        syno_paths=syno_paths,
    )
    write_task = asyncio.create_task(write_worker(q))
    meta_task = asyncio.create_task(metadata_worker(mq, cs.process_metadata_item))
    try:
        with patch.object(
            syno_paths,
            "list_children",
            side_effect=list_children_side_effect,
        ):
            stats = await bf(root_id, writer=cs)
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


class TestBackfillComparison(DecoyTestCase):
    """What Synology adds to the core comparison; the rest is in the core tests."""

    async def test_changed_locator_is_updated(self) -> None:
        # given: two mirrored files equal to their listing, one bound to an
        # old file_id
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(os.unlink, db_path)
        pool = self.enterContext(ThreadPoolExecutor())
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
                _node("same", "same.txt", "dir-1"),
                _node("moved", "moved.txt", "dir-1"),
            ]
        )
        ns = mirror.namespace("synology")
        await mirror.write(
            lambda con: [
                SYNOLOGY_SCHEMA.bind_locator(con, ns, NodeId(i), SynologyLocator(f))
                for i, f in (("same", "same"), ("moved", "old-file-id"))
            ]
        )

        async def _list_children(_nw: object, parent_id: str) -> list[dict]:
            if parent_id == "dir-1":
                return [
                    {**_syno_item(i, f"{i}.txt"), "change_time": _TS}
                    for i in ("same", "moved")
                ]
            return []

        # when
        stats = await _reconcile_with_worker(
            mirror,
            {"docs": "/volume1/docs"},
            self.decoy.mock(cls=SynologyClient),
            "dir-1",
            list_children_side_effect=_list_children,
        )

        # then
        self.assertEqual((stats.checked, stats.updated), (2, 1))
        locators = await SynologyStoreReader(mirror=mirror, ns=ns).get_locators(
            [NodeId("same"), NodeId("moved")]
        )
        self.assertEqual(
            locators,
            {
                NodeId("same"): SynologyLocator("same"),
                NodeId("moved"): SynologyLocator("moved"),
            },
        )

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
                    list_children_side_effect=_list_children,
                )

                # then
                self.assertIn(mount_id("docs"), listed)
                self.assertEqual(stats.checked, 1)
                self.assertEqual(stats.added, 1)
        finally:
            os.unlink(db_path)


class TestReadOnlyDryRun(DecoyTestCase):
    """A dry run on the read side alone reports the same and writes nothing."""

    async def test_dry_run_without_a_writer_with_provisional_ids(self) -> None:
        # given
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db_path = os.path.join(directory, "mirror.sqlite")
        pool = self.enterContext(ThreadPoolExecutor())
        off_main = OffMainService(pool=pool)
        writer = MirrorStore(
            db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        )
        await writer.ensure_schema()
        nodes = [
            _node(ROOT_ID, "", None, is_directory=True),
            _node(mount_id("docs"), "docs", ROOT_ID, is_directory=True),
            _node("dir-1", "Projects", mount_id("docs"), is_directory=True),
            _node("f-1", "keep.txt", "dir-1"),
            _node("f-2", "deleted.txt", "dir-1"),
        ]
        await writer.bulk_upsert_nodes(nodes)
        key = writer.namespace("synology").key
        con = sqlite3.connect(db_path, autocommit=True)
        con.executemany(
            "INSERT INTO node_ids (id, instance, native) VALUES (?, ?, ?)",
            [(n.id, key, n.id) for n in nodes[2:]],
        )
        con.close()
        checkpoint_database(db_path)
        before = {
            name: Path(directory, name).read_bytes()
            for name in sorted(os.listdir(directory))
        }

        mirror = MirrorStore(read_only_dsn(db_path), off_main=off_main)
        await mirror.check_schema()
        ns = await mirror.check_backend_schema("synology", SYNOLOGY_SCHEMA)
        ids = ProvisionalNodeIds(mirror=mirror, key=ns.key)
        store = SynologyStoreReader(mirror=mirror, ns=ns)
        syno_paths = SynologyPathService(
            ids=ids,
            registry=MountRegistry(
                mounts=make_mounts({"docs": "/volume1/docs"}), root_ids={}
            ),
            store=store,
        )
        backfill = partial(
            run_backfill,
            drive_api=self.decoy.mock(cls=SynologyClient),
            mirror=mirror,
            store=store,
            syno_paths=syno_paths,
            writer=None,
        )
        listings = {
            "dir-1": [
                _syno_item("f-1", "keep.txt"),
                _syno_item("d-new", "New", is_dir=True),
            ],
            "d-new": [_syno_item("f-deep", "deep.txt")],
        }

        async def _list_children(_api: object, parent_id: NodeId) -> list[dict]:
            # A folder seen for the first time is listed by its provisional ID.
            return listings.get(await ids.native_of(parent_id), [])

        # when
        with patch.object(syno_paths, "list_children", side_effect=_list_children):
            stats = await backfill(NodeId("dir-1"))

        # then
        self.assertEqual((stats.checked, stats.added, stats.removed), (3, 2, 1))
        after = {
            name: Path(directory, name).read_bytes()
            for name in sorted(os.listdir(directory))
        }
        self.assertEqual(after, before)

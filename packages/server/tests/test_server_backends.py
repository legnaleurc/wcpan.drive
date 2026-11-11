"""Several backend instances share one mirror without mixing their state."""

import asyncio
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.mounts import Mounts, ensure_mount_nodes, mount_id
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import BackendLocator, NodeObservation
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)


class _RecordingSchema:
    def __init__(self) -> None:
        self.bound: list[tuple[str, str]] = []

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        pass

    def bind_locator(
        self, con: sqlite3.Connection, node_id: NodeId, locator: BackendLocator
    ) -> None:
        self.bound.append((node_id, locator))

    def forget_nodes(self, con: sqlite3.Connection, node_ids: Sequence[str]) -> None:
        pass

    def forget_orphans(self, con: sqlite3.Connection) -> None:
        pass


def _file(node_id: str, parent_id: str) -> NodeRecord:
    return NodeRecord(
        id=NodeId(node_id),
        parent_id=NodeId(parent_id),
        name=node_id,
        is_directory=False,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type="text/plain",
        hash="abc",
        hash_algorithm="md5",
        size=1,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _mounts(*names: str) -> Mounts:
    return Mounts.from_sources({name: f"/{name}" for name in names})


class TestBackendViews(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.pool = ThreadPoolExecutor(2)
        self.a = _RecordingSchema()
        self.b = _RecordingSchema()
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"a": self.a, "b": self.b},
        )
        await self.mirror.ensure_schema()
        wq = create_write_queue()
        mq = create_metadata_queue()
        self.node_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=wq,
            off_main=OffMainService(pool=self.pool),
            mounts={},
            metadata_queue=mq,
        )
        self.tasks = [
            asyncio.create_task(write_worker(wq, noop_after_write)),
            asyncio.create_task(
                metadata_worker(mq, self.node_sync.process_metadata_item)
            ),
        ]
        await ensure_mount_nodes(
            _mounts("one", "two"), mirror=self.mirror, node_sync=self.node_sync
        )

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.pool.shutdown(wait=False, cancel_futures=True)
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                Path(self.db_path + suffix).unlink()

    async def test_locators_bind_with_their_backend_schema(self) -> None:
        # given
        view_a = self.node_sync.for_backend("a")
        view_b = self.node_sync.for_backend("b")
        # when
        async with view_a.observe() as read:
            await view_a.upsert(
                NodeObservation(
                    record=_file("fa", mount_id("one")), locator=BackendLocator("la")
                ),
                read=read,
            )
        async with view_b.observe() as read:
            await view_b.upsert(
                NodeObservation(
                    record=_file("fb", mount_id("two")), locator=BackendLocator("lb")
                ),
                read=read,
            )
        await self.node_sync.wait_enrichment_drained()

        # then
        self.assertEqual(self.a.bound, [("fa", "la")])
        self.assertEqual(self.b.bound, [("fb", "lb")])

    async def test_each_backend_retries_only_its_pending_media(self) -> None:
        # given
        await self.mirror.mark_enrichment_pending(NodeId("fa"), "a")
        await self.mirror.mark_enrichment_pending(NodeId("fb"), "b")
        located: list[str] = []

        async def locate(node_id: NodeId) -> None:
            located.append(node_id)

        # when
        await self.node_sync.for_backend("a").retry_pending_enrichment(locate=locate)
        await self.node_sync.wait_enrichment_drained()

        # then
        self.assertEqual(located, ["fa"])
        self.assertEqual(await self.mirror.get_pending_enrichment("b"), ["fb"])

    async def test_new_mounts_are_published_once(self) -> None:
        # given
        cursor = await self.mirror.get_cursor()
        # when
        await ensure_mount_nodes(
            _mounts("one", "two"), mirror=self.mirror, node_sync=self.node_sync
        )
        # then
        self.assertEqual(await self.mirror.get_cursor(), cursor)

        # when
        await ensure_mount_nodes(
            _mounts("one", "two", "three"),
            mirror=self.mirror,
            node_sync=self.node_sync,
        )
        # then
        changes, _, _ = await self.mirror.get_changes_since(cursor, 100)
        self.assertEqual([c[0] for c in changes], [mount_id("three")])

"""Several backend instances share one mirror without mixing their state."""

import asyncio
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib.root import SERVER_ROOT_ID
from wcpan.drive.server.services.mirror import (
    BackendNamespace,
    MirrorStore,
    SchemaVersionError,
)
from wcpan.drive.server.services.mounts import Mounts, ensure_mount_nodes
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

from ._mounts import make_mounts, mount_id


class _RecordingSchema:
    name = "recording"

    def __init__(self, version: int = 1) -> None:
        self.version = version
        self.bound: list[tuple[str, str]] = []

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        pass

    def bind_locator(
        self,
        con: sqlite3.Connection,
        ns: BackendNamespace,
        node_id: NodeId,
        locator: BackendLocator,
    ) -> None:
        self.bound.append((node_id, locator))

    def forget_nodes(
        self, con: sqlite3.Connection, ns: BackendNamespace, node_ids: Sequence[str]
    ) -> None:
        pass

    def forget_orphans(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        pass


class _OtherSchema(_RecordingSchema):
    name = "other"


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
    return make_mounts({name: f"/{name}" for name in names})


class TestRootPlacement(IsolatedAsyncioTestCase):
    """Only the core places nodes at the root or directly under it."""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.pool = ThreadPoolExecutor(1)
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"a": _RecordingSchema()},
        )
        await self.mirror.ensure_schema()

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                Path(self.db_path + suffix).unlink()

    async def test_backend_node_under_the_root_is_refused(self) -> None:
        # given
        observation = NodeObservation(
            record=_file("fx", SERVER_ROOT_ID), locator=None, backend="a"
        )
        # when / then
        with self.assertRaises(ValueError):
            await self.mirror.apply_observations([observation])
        self.assertIsNone(await self.mirror.get_node_by_id(NodeId("fx")))

    async def test_backend_node_without_parent_is_refused(self) -> None:
        # given
        observation = NodeObservation(
            record=replace(_file("fx", "p"), parent_id=None),
            locator=None,
            backend="a",
        )
        # when / then
        with self.assertRaises(ValueError):
            await self.mirror.apply_observations([observation])
        self.assertIsNone(await self.mirror.get_node_by_id(NodeId("fx")))

    async def test_core_places_root_and_its_children(self) -> None:
        # given
        root = replace(_file(SERVER_ROOT_ID, "p"), parent_id=None)
        mount = _file("m", SERVER_ROOT_ID)
        # when
        await self.mirror.apply_observations(
            [
                NodeObservation(record=root, locator=None),
                NodeObservation(record=mount, locator=None),
            ]
        )
        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(NodeId("m")))


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

    async def test_observed_nodes_belong_to_their_backend(self) -> None:
        # given
        view_a = self.node_sync.for_backend("a")
        # when
        async with view_a.observe() as read:
            await view_a.upsert(
                NodeObservation(record=_file("fa", mount_id("one")), locator=None),
                read=read,
            )
        await self.node_sync.wait_enrichment_drained()
        await self.mirror.mark_enrichment_pending(NodeId("withheld"), "b")

        # then: withheld nodes too; mounts belong to the core
        self.assertEqual(await self.mirror.owner_of(NodeId("fa")), "a")
        self.assertEqual(await self.mirror.owner_of(NodeId("withheld")), "b")
        self.assertIsNone(await self.mirror.owner_of(mount_id("one")))

    async def test_unregistered_backend_cannot_withhold(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            await self.mirror.mark_enrichment_pending(NodeId("fx"), "unknown")
        self.assertEqual(await self.mirror.get_pending_enrichment("unknown"), [])

    async def test_observation_of_unregistered_backend_is_refused(self) -> None:
        # given
        observation = NodeObservation(
            record=_file("fx", mount_id("one")), locator=None, backend="unknown"
        )
        # when / then
        with self.assertRaises(ValueError):
            await self.mirror.apply_observations([observation])
        self.assertIsNone(await self.mirror.get_node_by_id(NodeId("fx")))

    async def test_untagged_locator_is_refused(self) -> None:
        # given
        observation = NodeObservation(
            record=_file("fx", mount_id("one")), locator=BackendLocator("lx")
        )
        # when / then
        with self.assertRaises(ValueError):
            await self.mirror.apply_observations([observation])
        self.assertIsNone(await self.mirror.get_node_by_id(NodeId("fx")))
        self.assertEqual((self.a.bound, self.b.bound), ([], []))

    async def test_instances_have_their_own_namespaces(self) -> None:
        a = self.mirror.namespace("a")
        b = self.mirror.namespace("b")
        self.assertNotEqual(a.key, b.key)
        self.assertRegex(a.key, r"^[0-9a-f]+$")

    async def test_registrations_survive_restart(self) -> None:
        # given
        ns = self.mirror.namespace("a")
        # when
        mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await mirror.ensure_schema()
        # then
        self.assertEqual(await mirror.register_schema("a", _RecordingSchema()), ns)
        self.assertEqual(await mirror.registered_instances(), ["a", "b"])

    async def test_rejects_another_schema_version(self) -> None:
        mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await mirror.ensure_schema()
        with self.assertRaises(SchemaVersionError):
            await mirror.register_schema("a", _RecordingSchema(version=2))

    async def test_schema_version_is_shared_by_instances(self) -> None:
        # a new instance uses the tables the backend already has
        mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await mirror.ensure_schema()
        with self.assertRaises(SchemaVersionError):
            await mirror.register_schema("c", _RecordingSchema(version=2))

    async def test_rejects_another_backend_schema(self) -> None:
        # given: "a" holds rows of the recording backend's tables
        mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await mirror.ensure_schema()
        # when / then
        with self.assertRaises(SchemaVersionError):
            await mirror.register_schema("a", _OtherSchema())

    async def test_new_instance_takes_any_schema(self) -> None:
        # registered before its backend loads, so no schema is bound yet
        await self.mirror.register_instance("c")
        await self.mirror.register_schema("c", _OtherSchema())
        mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await mirror.ensure_schema()
        with self.assertRaises(SchemaVersionError):
            await mirror.register_schema("c", _RecordingSchema())

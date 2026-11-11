"""Mount lookup by name and node ID, and routing by containing mount."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import IsolatedAsyncioTestCase, TestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID, is_structural
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.mounts import (
    Mounts,
    ensure_mount_nodes,
    load_mounts,
)
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.operations import (
    NodeOperations,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.routing import BackendRouter
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    noop_after_write,
    write_worker,
)


def _dir(node_id: str, parent_id: str | None) -> NodeRecord:
    return NodeRecord(
        id=NodeId(node_id),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
        name=node_id,
        is_directory=True,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type="",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class _Ancestors:
    """``get_ancestors`` over a fixed tree, stopping below mount nodes."""

    def __init__(self, mounts: Mounts, records: list[NodeRecord]) -> None:
        self._mounts = mounts
        self._records = {r.id: r for r in records}
        self.queried: list[NodeId] = []

    async def get_ancestors(self, node_id: NodeId) -> list[NodeRecord]:
        self.queried.append(node_id)
        chain: list[NodeRecord] = []
        current = self._records.get(node_id)
        while current is not None:
            chain.append(current)
            parent = current.parent_id
            if (
                parent is None
                or parent == self._mounts.root_id
                or self._mounts.by_id(parent) is not None
            ):
                break
            current = self._records.get(parent)
        return chain


_ALL = Mounts.from_sources({"docs": "/docs", "pics": "/pics"})
_DOCS = _ALL["docs"]
_PICS = _ALL["pics"]


class TestMounts(TestCase):
    def test_lookup_by_name_and_id(self) -> None:
        # then
        self.assertEqual(list(_ALL), ["docs", "pics"])
        self.assertEqual(_DOCS.source, "/docs")
        self.assertIs(_ALL.by_id(_DOCS.id), _DOCS)
        self.assertIsNone(_ALL.by_id(_ALL.root_id))
        self.assertIsNone(_ALL.by_id(NodeId("docs")))

    def test_subset_serves_only_its_mounts(self) -> None:
        # when
        subset = _ALL.subset(["pics"])
        # then
        self.assertEqual(list(subset), ["pics"])
        self.assertIsNone(subset.by_id(_DOCS.id))
        self.assertIs(subset.by_id(_PICS.id), _PICS)

    def test_chain_ends_under_its_mount(self) -> None:
        # given
        chain = [_dir("file", "folder"), _dir("folder", _DOCS.id)]
        # then
        self.assertIs(_ALL.of_chain(chain), _DOCS)
        self.assertIsNone(_ALL.subset(["pics"]).of_chain(chain))

    def test_empty_or_detached_chain_has_no_mount(self) -> None:
        # then
        self.assertIsNone(_ALL.of_chain([]))
        self.assertIsNone(_ALL.of_chain([_dir("orphan", None)]))
        self.assertIsNone(_ALL.of_chain([_dir("lost", "missing")]))


class TestContaining(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mirror = _Ancestors(
            _ALL,
            [
                _dir(_ALL.root_id, None),
                _dir(_DOCS.id, _ALL.root_id),
                _dir(_PICS.id, _ALL.root_id),
                _dir("folder", _DOCS.id),
                _dir("file", "folder"),
                _dir("photo", _PICS.id),
            ],
        )

    async def _containing(self, mounts: Mounts, node_id: str):
        return await mounts.containing(
            NodeId(node_id), mirror=cast(MirrorStore, self.mirror)
        )

    async def test_mount_node_is_its_own_mount(self) -> None:
        # when
        mount = await self._containing(_ALL, _DOCS.id)
        # then
        self.assertIs(mount, _DOCS)
        self.assertEqual(self.mirror.queried, [])

    async def test_descendant_is_in_its_mount(self) -> None:
        # then
        self.assertIs(await self._containing(_ALL, "file"), _DOCS)
        self.assertIs(await self._containing(_ALL, "photo"), _PICS)

    async def test_nodes_of_other_mounts_are_outside_a_subset(self) -> None:
        # given
        docs_only = _ALL.subset(["docs"])
        # then
        self.assertIsNone(await self._containing(docs_only, "photo"))
        self.assertIsNone(await self._containing(docs_only, _PICS.id))
        self.assertIs(await self._containing(docs_only, "file"), _DOCS)

    async def test_root_and_unknown_nodes_have_no_mount(self) -> None:
        # then
        self.assertIsNone(await self._containing(_ALL, _ALL.root_id))
        self.assertIsNone(await self._containing(_ALL, "unknown"))


class TestBackendOf(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        mirror = _Ancestors(
            _ALL,
            [
                _dir(_DOCS.id, _ALL.root_id),
                _dir("folder", _DOCS.id),
                _dir("photo", _PICS.id),
            ],
        )
        self.router = BackendRouter(
            mirror=cast(MirrorStore, mirror),
            mounts=_ALL.subset(["docs", "pics"]),
            mount_backends={"docs": "disk"},
            operations={"disk": cast(NodeOperations, object())},
            uploaders={},
            files={},
        )

    async def test_node_routes_to_the_backend_of_its_mount(self) -> None:
        # then
        self.assertEqual(await self.router.backend_of(_DOCS.id), "disk")
        self.assertEqual(await self.router.backend_of(NodeId("folder")), "disk")

    async def test_server_root_is_not_supported(self) -> None:
        # when / then
        with self.assertRaises(OperationNotSupportedError):
            await self.router.backend_of(_ALL.root_id)

    async def test_mount_without_backend_is_unavailable(self) -> None:
        # when / then
        with self.assertRaises(OperationUnavailableError):
            await self.router.backend_of(NodeId("photo"))
        with self.assertRaises(OperationUnavailableError):
            await self.router.backend_of(NodeId("unknown"))


class TestMountNodes(IsolatedAsyncioTestCase):
    """Mount nodes are found by name and recognized by position, not by ID."""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.pool = ThreadPoolExecutor(2)
        self.mirror = MirrorStore(
            self.db_path, off_main=OffMainService(pool=self.pool), schemas={}
        )
        await self.mirror.ensure_schema()
        # An opaque mount ID, and a real node whose ID looks like the old grammar.
        await self.mirror.bulk_upsert_nodes(
            [
                _dir(SERVER_ROOT_ID, None),
                replace(_dir("m1", SERVER_ROOT_ID), name="docs"),
                _dir("_folder", "m1"),
                _dir("file", "_folder"),
            ]
        )

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                Path(self.db_path + suffix).unlink()

    async def test_existing_mount_keeps_its_node_and_new_ones_get_ids(self) -> None:
        # when
        mounts = await load_mounts(
            {"docs": "/docs", "new": "/new", "other": "/other"}, mirror=self.mirror
        )
        # then
        self.assertEqual(mounts["docs"].id, "m1")
        new_ids = {mounts["new"].id, mounts["other"].id}
        self.assertEqual(len(new_ids), 2)
        self.assertNotIn("m1", new_ids)
        self.assertNotIn(SERVER_ROOT_ID, new_ids)

    async def test_ancestors_stop_below_the_mount_node(self) -> None:
        # when
        chain = await self.mirror.get_ancestors(NodeId("file"))
        # then
        self.assertEqual([r.id for r in chain], ["file", "_folder"])
        mounts = await load_mounts({"docs": "/docs"}, mirror=self.mirror)
        self.assertIs(mounts.of_chain(chain), mounts["docs"])

    async def test_removal_roots_exclude_nothing_by_id_prefix(self) -> None:
        # when
        roots = await self.mirror.deferred_removal_roots(set(), {NodeId("m1")})
        # then
        self.assertEqual(roots, ["_folder"])

    async def test_duplicate_mount_nodes_are_rejected(self) -> None:
        # given
        await self.mirror.bulk_upsert_nodes(
            [replace(_dir("m2", SERVER_ROOT_ID), name="docs")]
        )
        # when / then
        with self.assertRaisesRegex(ValueError, "docs"):
            await load_mounts({"docs": "/docs"}, mirror=self.mirror)

    async def test_duplicates_of_unconfigured_names_are_ignored(self) -> None:
        # given
        await self.mirror.bulk_upsert_nodes(
            [
                replace(_dir("o1", SERVER_ROOT_ID), name="old"),
                replace(_dir("o2", SERVER_ROOT_ID), name="old"),
            ]
        )
        # when
        mounts = await load_mounts({"docs": "/docs"}, mirror=self.mirror)
        # then
        self.assertEqual(mounts["docs"].id, "m1")

    async def test_root_and_mounts_are_structural(self) -> None:
        # given
        records = await self.mirror.get_nodes_by_ids(
            [SERVER_ROOT_ID, NodeId("m1"), NodeId("_folder")]
        )
        # then
        self.assertTrue(is_structural(records[SERVER_ROOT_ID]))
        self.assertTrue(is_structural(records[NodeId("m1")]))
        self.assertFalse(is_structural(records[NodeId("_folder")]))


class TestMountRestart(IsolatedAsyncioTestCase):
    """A new mount keeps its assigned ID once its node is written."""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.pool = ThreadPoolExecutor(2)
        self.mirror = MirrorStore(
            self.db_path, off_main=OffMainService(pool=self.pool), schemas={}
        )
        await self.mirror.ensure_schema()
        write_queue = create_write_queue()
        self.node_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=write_queue,
            off_main=OffMainService(pool=self.pool),
            mounts={},
            metadata_queue=create_metadata_queue(),
        )
        self.writer = asyncio.create_task(write_worker(write_queue, noop_after_write))

    async def asyncTearDown(self) -> None:
        self.writer.cancel()
        with suppress(asyncio.CancelledError):
            await self.writer
        self.pool.shutdown(wait=False, cancel_futures=True)
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                Path(self.db_path + suffix).unlink()

    async def _start(self, sources: dict[str, str]) -> Mounts:
        mounts = await load_mounts(sources, mirror=self.mirror)
        await ensure_mount_nodes(mounts, mirror=self.mirror, node_sync=self.node_sync)
        return mounts

    async def test_restart_keeps_ids_and_publishes_nothing(self) -> None:
        # given
        first = await self._start({"docs": "/docs", "pics": "/pics"})
        cursor = await self.mirror.get_cursor()
        # when
        second = await self._start({"docs": "/docs", "pics": "/pics"})
        # then
        self.assertEqual(second["docs"].id, first["docs"].id)
        self.assertEqual(second["pics"].id, first["pics"].id)
        self.assertEqual(await self.mirror.get_cursor(), cursor)

    async def test_loading_alone_writes_nothing(self) -> None:
        # given
        cursor = await self.mirror.get_cursor()
        # when
        unwritten = await load_mounts({"docs": "/docs"}, mirror=self.mirror)
        # then
        self.assertIsNone(await self.mirror.get_node_by_id(unwritten["docs"].id))
        self.assertEqual(await self.mirror.get_cursor(), cursor)

        # when
        started = await self._start({"docs": "/docs"})
        # then
        self.assertNotEqual(started["docs"].id, unwritten["docs"].id)

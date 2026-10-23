"""Regression tests for stale observations racing newer mirror writes."""

import asyncio
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID, MountRegistry, mount_id
from wcpan.drive.server.services.backfill import BackfillService
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import SynologyPathService
from wcpan.drive.server.services.scan import StartupScanService
from wcpan.drive.server.services.storage import StorageService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation, ReadSeq, SynologyLocator
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)
from wcpan.synology import SynologyApiError, SynologyNetworkError, SynologyPath


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)

_TS = 1_700_000_000
_MOUNT = mount_id("docs")


def _node(
    node_id: str,
    parent_id: str | None,
    *,
    name: str | None = None,
    is_directory: bool = False,
) -> NodeRecord:
    return NodeRecord(
        id=MirrorStableId(node_id),
        parent_id=MirrorStableId(parent_id) if parent_id is not None else None,
        name=name if name is not None else node_id,
        is_directory=is_directory,
        created_time=_TS,
        modified_time=_TS,
        changed_time=_TS,
        mime_type="application/x-directory" if is_directory else "text/plain",
        hash="" if is_directory else "abc",
        hash_algorithm="" if is_directory else "md4",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _obs(record: NodeRecord) -> NodeObservation:
    return NodeObservation(record=record, locator=SynologyLocator(record.id))


def _syno_item(node_id: str, *, is_dir: bool = False) -> dict[str, object]:
    return {
        "file_id": node_id,
        "permanent_link": node_id,
        "parent_id": "",
        "name": node_id,
        "type": "dir" if is_dir else "file",
        "content_type": "dir" if is_dir else "file",
        "size": 0,
        "created_time": _TS,
        "modified_time": _TS,
        "change_time": 0,
        "sync_id": 1,
    }


class _Gate:
    """Blocks enrichment of selected records until released."""

    def __init__(self, predicate: Callable[[NodeRecord], bool]) -> None:
        self._predicate = predicate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def enrich(
        self, record: NodeRecord, *, force_refresh: bool = False
    ) -> NodeRecord:
        if self._predicate(record):
            self.entered.set()
            await self.release.wait()
        return record


class _RaceTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor(2)
        off_main = OffMainService(pool=self.pool)
        self.storage = StorageService(self.db_path, off_main=off_main)
        await self.storage.ensure_schema()
        await self.storage.bulk_upsert_nodes(
            [
                _node(SERVER_ROOT_ID, None, name="", is_directory=True),
                _node(_MOUNT, SERVER_ROOT_ID, name="docs", is_directory=True),
            ]
        )
        self.write_queue = create_write_queue()
        self.metadata_queue = create_metadata_queue()
        self.mounts = {"docs": SynologyPath("/docs")}
        self.node_sync = NodeSyncService(
            storage=self.storage,
            write_queue=self.write_queue,
            off_main=off_main,
            mounts=self.mounts,
            local_paths={},
            metadata_queue=self.metadata_queue,
        )
        self.tasks = [
            asyncio.create_task(write_worker(self.write_queue, noop_after_write)),
            asyncio.create_task(
                metadata_worker(
                    self.metadata_queue, self.node_sync.process_metadata_item
                )
            ),
            asyncio.create_task(
                metadata_worker(
                    self.metadata_queue, self.node_sync.process_metadata_item
                )
            ),
        ]

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    def gate(self, predicate: Callable[[NodeRecord], bool]) -> _Gate:
        gate = _Gate(predicate)
        patcher = patch.object(self.node_sync._enricher, "enrich", gate.enrich)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gate

    async def drain(self) -> None:
        await self.node_sync.wait_enrichment_drained()

    async def observed(self, op: Callable[[ReadSeq], Awaitable[object]]) -> None:
        """Run *op* as one backend read; the read starts when this coroutine runs."""
        async with self.node_sync.observe() as read:
            await op(read)

    async def run_gated(
        self, gate: _Gate, start: Awaitable[object], race: Callable[[], Awaitable[None]]
    ) -> None:
        """Start gated work, apply *race* while it is blocked, then release it."""
        # Fail instead of hanging when the gated work errors before reaching the gate.
        async with asyncio.timeout(5):
            task = asyncio.ensure_future(start)
            await gate.entered.wait()
            await race()
            await self.node_sync.flush()
            gate.release.set()
            await task
            await self.drain()


class TestStaleEnrichment(_RaceTestCase):
    async def test_pending_enrichment_does_not_resurrect_deleted_node(self) -> None:
        f = _node("f", _MOUNT)
        await self.storage.bulk_upsert_nodes([f])
        gate = self.gate(lambda r: r.id == "f")

        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.reconcile_insert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(f.id, read=r)),
        )

        self.assertIsNone(await self.storage.get_node_by_id(f.id))

    async def test_pending_enrichment_does_not_overwrite_newer_upsert(self) -> None:
        old = _node("f", _MOUNT, name="old")
        new = replace(old, name="new")
        gate = self.gate(lambda r: r.name == "old")

        await self.run_gated(
            gate,
            self.observed(
                lambda r: self.node_sync.upsert_file_batch([_obs(old)], read=r)
            ),
            lambda: self.observed(lambda r: self.node_sync.upsert(_obs(new), read=r)),
        )

        stored = await self.storage.get_node_by_id(old.id)
        assert stored is not None
        self.assertEqual(stored.name, "new")

    async def test_pending_enrichment_does_not_recreate_child_of_deleted_parent(
        self,
    ) -> None:
        d = _node("d", _MOUNT, is_directory=True)
        f = _node("f", "d")
        await self.storage.bulk_upsert_nodes([d])
        gate = self.gate(lambda r: r.id == "f")

        await self.run_gated(
            gate,
            self.observed(
                lambda r: self.node_sync.upsert_file_batch([_obs(f)], read=r)
            ),
            lambda: self.observed(lambda r: self.node_sync.delete(d.id, read=r)),
        )

        self.assertIsNone(await self.storage.get_node_by_id(f.id))

    async def test_inline_upsert_does_not_resurrect_concurrently_deleted_node(
        self,
    ) -> None:
        f = _node("f", _MOUNT)
        await self.storage.bulk_upsert_nodes([f])
        gate = self.gate(lambda r: r.id == "f")

        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.upsert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(f.id, read=r)),
        )

        self.assertIsNone(await self.storage.get_node_by_id(f.id))

    async def test_unrelated_pending_enrichment_still_applies(self) -> None:
        f = _node("f", _MOUNT)
        g = _node("g", _MOUNT)
        await self.storage.bulk_upsert_nodes([g])
        gate = self.gate(lambda r: r.id == "f")

        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.reconcile_insert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(g.id, read=r)),
        )

        self.assertIsNotNone(await self.storage.get_node_by_id(f.id))
        self.assertIsNone(await self.storage.get_node_by_id(g.id))


class TestBackfillOrdering(_RaceTestCase):
    def backfill(self) -> tuple[BackfillService, SynologyPathService]:
        syno_paths = SynologyPathService(
            registry=MountRegistry(mounts=self.mounts, root_ids={}),
            storage=self.storage,
        )
        backfill = BackfillService(
            drive_api=object(),  # type: ignore[arg-type]
            storage=self.storage,
            syno_paths=syno_paths,
            node_sync=self.node_sync,
        )
        return backfill, syno_paths

    async def test_listing_of_former_parent_does_not_delete_moved_node(self) -> None:
        a = _node("a", _MOUNT, is_directory=True)
        b = _node("b", _MOUNT, is_directory=True)
        x = _node("x", "a")
        await self.storage.bulk_upsert_nodes([a, b, x])
        backfill, syno_paths = self.backfill()

        async def list_children(_api: object, parent_id: str) -> list[object]:
            return [] if parent_id == "a" else [_syno_item("x")]

        real_get_children = self.storage.get_children

        async def get_children(parent_id: MirrorStableId) -> list[NodeRecord]:
            children = await real_get_children(parent_id)
            if parent_id == "a":
                # A webhook commits the move after backfill read the old parent.
                await self.storage.upsert_node_and_emit_change(
                    replace(x, parent_id=b.id)
                )
            return children

        with (
            patch.object(syno_paths, "list_children", side_effect=list_children),
            patch.object(self.storage, "get_children", side_effect=get_children),
        ):
            await backfill._reconcile_subtree(a.id)
        await self.drain()

        stored = await self.storage.get_node_by_id(x.id)
        assert stored is not None
        self.assertEqual(stored.parent_id, b.id)

    async def test_stale_listing_does_not_revert_newer_webhook_update(self) -> None:
        a = _node("a", _MOUNT, is_directory=True)
        x = _node("x", "a")
        await self.storage.bulk_upsert_nodes([a, x])
        backfill, syno_paths = self.backfill()
        renamed = replace(x, name="renamed")

        async def list_children(_api: object, parent_id: str) -> list[object]:
            # The listing still reports the old name; a webhook read that starts
            # afterwards observes the rename and is applied first.
            async with self.node_sync.observe() as read:
                await self.node_sync.upsert(_obs(renamed), read=read)
            await self.node_sync.flush()
            return [_syno_item("x")]

        with patch.object(syno_paths, "list_children", side_effect=list_children):
            await backfill._reconcile_subtree(a.id)
        await self.drain()

        stored = await self.storage.get_node_by_id(x.id)
        assert stored is not None
        self.assertEqual(stored.name, "renamed")


class TestScanRemovalConfirmation(_RaceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.a = _node("a", _MOUNT, is_directory=True)
        self.b = _node("b", _MOUNT, is_directory=True)
        self.x = _node("x", "a")
        await self.storage.apply_observations(
            [_obs(self.a), _obs(self.b), _obs(self.x)]
        )
        self.drive_api = MagicMock()
        self.scan = StartupScanService(
            drive_api=self.drive_api,
            storage=self.storage,
            syno_paths=SynologyPathService(
                registry=MountRegistry(mounts=self.mounts, root_ids={}),
                storage=self.storage,
            ),
            node_sync=self.node_sync,
        )

    async def test_node_moved_during_scan_is_kept_under_new_parent(self) -> None:
        self.drive_api.get_file = AsyncMock(
            return_value={**_syno_item("x"), "parent_id": "b"}
        )

        await self.scan._remove_unless_moved(self.x.id)
        await self.drain()

        stored = await self.storage.get_node_by_id(self.x.id)
        assert stored is not None
        self.assertEqual(stored.parent_id, self.b.id)

    async def test_missing_node_is_removed(self) -> None:
        self.drive_api.get_file = AsyncMock(return_value=None)

        await self.scan._remove_unless_moved(self.x.id)
        await self.drain()

        self.assertIsNone(await self.storage.get_node_by_id(self.x.id))

    async def test_api_error_falls_back_to_removal(self) -> None:
        self.drive_api.get_file = AsyncMock(
            side_effect=SynologyApiError("not found", error_code=408)
        )

        await self.scan._remove_unless_moved(self.x.id)
        await self.drain()

        self.assertIsNone(await self.storage.get_node_by_id(self.x.id))

    async def test_node_moved_outside_mounts_is_removed(self) -> None:
        self.drive_api.get_file = AsyncMock(
            return_value={**_syno_item("x"), "parent_id": "elsewhere"}
        )

        await self.scan._remove_unless_moved(self.x.id)
        await self.drain()

        self.assertIsNone(await self.storage.get_node_by_id(self.x.id))

    async def test_network_failure_preserves_node(self) -> None:
        self.drive_api.get_file = AsyncMock(side_effect=SynologyNetworkError("down"))

        await self.scan._remove_unless_moved(self.x.id)
        await self.drain()

        self.assertIsNotNone(await self.storage.get_node_by_id(self.x.id))

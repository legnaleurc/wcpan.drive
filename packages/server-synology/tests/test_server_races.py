"""Regression tests for stale observations racing newer mirror writes."""

import asyncio
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from decoy import matchers
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._lib.write_queue import (
    create_write_queue,
    write_worker,
)
from wcpan.drive.server._services.media.enricher import MediaEnrichmentError
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import ProbeFailure
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.media.retries import MediaRetries
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.operations import LocateUndecidedError
from wcpan.drive.server._services.sync.metadata import (
    create_metadata_queue,
    metadata_worker,
)
from wcpan.drive.server._services.sync.reconcile import reconcile_session
from wcpan.drive.server._services.sync.service import NodeSyncService
from wcpan.drive.server._types import (
    NodeObservation,
    ReadSeq,
)
from wcpan.drive.server.synology._backfill import BackfillService
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.synology.exceptions import (
    SynologyApiError,
    SynologyDirectoryNotFoundError,
    SynologyNetworkError,
    SynologyNotFoundError,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyFileInfo,
    SynologyPath,
    SynologyPermanentLink,
)

from ._lib import ROOT_ID, DecoyTestCase, IdentityIds, mount_id, mounts_of


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
        id=NodeId(node_id),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
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
    )


def _obs(record: NodeRecord) -> NodeObservation:
    # Test IDs are their own permanent links (``IdentityIds``).
    return NodeObservation(
        record=record,
        locator=SynologyLocator(record.id),
        backend="synology",
        source_key=record.id,
    )


def _syno_item(
    node_id: str, *, is_dir: bool = False, parent_id: str = ""
) -> SynologyFileInfo:
    return {
        "file_id": node_id,
        "permanent_link": node_id,
        "parent_id": parent_id,
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
        self, observation: NodeObservation, *, force_refresh: bool = False
    ) -> NodeRecord:
        record = observation.record
        if self._predicate(record):
            self.entered.set()
            await self.release.wait()
        return record


class _RaceTestCase(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor(2)
        off_main = OffMainService(pool=self.pool)
        self.mirror = MirrorStore(
            self.db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        )
        await self.mirror.ensure_schema()
        await self.mirror.bulk_upsert_nodes(
            [
                _node(ROOT_ID, None, name="", is_directory=True),
                _node(_MOUNT, ROOT_ID, name="docs", is_directory=True),
            ]
        )
        self.write_queue = create_write_queue()
        self.metadata_queue = create_metadata_queue()
        self.mounts = {"docs": SynologyPath("/docs")}
        self.node_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=self.write_queue,
            probing=MediaProbing(runner=ProbeRunner()),
            metadata_queue=self.metadata_queue,
        ).for_backend("synology")
        self.tasks = [
            asyncio.create_task(write_worker(self.write_queue)),
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
        # given
        f = _node("f", _MOUNT)
        await self.mirror.bulk_upsert_nodes([f])
        gate = self.gate(lambda r: r.id == "f")

        # when
        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.reconcile_insert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(f.id, read=r)),
        )

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(f.id))

    async def test_pending_enrichment_does_not_overwrite_newer_upsert(self) -> None:
        # given
        old = _node("f", _MOUNT, name="old")
        new = replace(old, name="new")
        gate = self.gate(lambda r: r.name == "old")

        # when
        await self.run_gated(
            gate,
            self.observed(
                lambda r: self.node_sync.upsert_file_batch([_obs(old)], read=r)
            ),
            lambda: self.observed(lambda r: self.node_sync.upsert(_obs(new), read=r)),
        )

        # then
        stored = await self.mirror.get_node_by_id(old.id)
        assert stored is not None
        self.assertEqual(stored.name, "new")

    async def test_pending_enrichment_does_not_recreate_child_of_deleted_parent(
        self,
    ) -> None:
        # given
        d = _node("d", _MOUNT, is_directory=True)
        f = _node("f", "d")
        await self.mirror.bulk_upsert_nodes([d])
        gate = self.gate(lambda r: r.id == "f")

        # when
        await self.run_gated(
            gate,
            self.observed(
                lambda r: self.node_sync.upsert_file_batch([_obs(f)], read=r)
            ),
            lambda: self.observed(lambda r: self.node_sync.delete(d.id, read=r)),
        )

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(f.id))

    async def test_inline_upsert_does_not_resurrect_concurrently_deleted_node(
        self,
    ) -> None:
        # given
        f = _node("f", _MOUNT)
        await self.mirror.bulk_upsert_nodes([f])
        gate = self.gate(lambda r: r.id == "f")

        # when
        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.upsert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(f.id, read=r)),
        )

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(f.id))

    async def test_unrelated_pending_enrichment_still_applies(self) -> None:
        # given
        f = _node("f", _MOUNT)
        g = _node("g", _MOUNT)
        await self.mirror.bulk_upsert_nodes([g])
        gate = self.gate(lambda r: r.id == "f")

        # when
        await self.run_gated(
            gate,
            self.observed(lambda r: self.node_sync.reconcile_insert(_obs(f), read=r)),
            lambda: self.observed(lambda r: self.node_sync.delete(g.id, read=r)),
        )

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(f.id))
        self.assertIsNone(await self.mirror.get_node_by_id(g.id))


class TestBackfillOrdering(_RaceTestCase):
    def backfill(self) -> tuple[BackfillService, SynologyPathService]:
        syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=mounts_of(self.mounts), root_ids={}),
            store=SynologyStore(
                mirror=self.mirror, ns=self.mirror.namespace("synology")
            ),
        )
        backfill = BackfillService(
            drive_api=object(),  # type: ignore[arg-type]
            mirror=self.mirror,
            store=SynologyStore(
                mirror=self.mirror, ns=self.mirror.namespace("synology")
            ),
            syno_paths=syno_paths,
            node_sync=self.node_sync,
        )
        return backfill, syno_paths

    async def test_listing_of_former_parent_does_not_delete_moved_node(self) -> None:
        # given
        a = _node("a", _MOUNT, is_directory=True)
        b = _node("b", _MOUNT, is_directory=True)
        x = _node("x", "a")
        await self.mirror.bulk_upsert_nodes([a, b, x])
        backfill, syno_paths = self.backfill()

        async def list_children(_api: object, parent_id: str) -> list[object]:
            return [] if parent_id == "a" else [_syno_item("x")]

        real_get_children = self.mirror.get_children

        async def get_children(parent_id: NodeId) -> list[NodeRecord]:
            children = await real_get_children(parent_id)
            if parent_id == "a":
                # A webhook commits the move after backfill read the old parent.
                await self.mirror.upsert_node_and_emit_change(
                    replace(x, parent_id=b.id)
                )
            return children

        # when
        with (
            patch.object(syno_paths, "list_children", side_effect=list_children),
            patch.object(self.mirror, "get_children", side_effect=get_children),
        ):
            await backfill._reconcile_subtree(a.id)
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(x.id)
        assert stored is not None
        self.assertEqual(stored.parent_id, b.id)

    async def test_stale_listing_does_not_revert_newer_webhook_update(self) -> None:
        # given
        a = _node("a", _MOUNT, is_directory=True)
        x = _node("x", "a")
        await self.mirror.bulk_upsert_nodes([a, x])
        backfill, syno_paths = self.backfill()
        renamed = replace(x, name="renamed")

        async def list_children(_api: object, parent_id: str) -> list[object]:
            # The listing still reports the old name; a webhook read that starts
            # afterwards observes the rename and is applied first.
            async with self.node_sync.observe() as read:
                await self.node_sync.upsert(_obs(renamed), read=read)
            await self.node_sync.flush()
            return [_syno_item("x")]

        # when
        with patch.object(syno_paths, "list_children", side_effect=list_children):
            await backfill._reconcile_subtree(a.id)
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(x.id)
        assert stored is not None
        self.assertEqual(stored.name, "renamed")


class TestReconcileRemovalConfirmation(_RaceTestCase):
    """Nodes no listing reported are fetched directly before removal."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.a = _node("a", _MOUNT, is_directory=True)
        self.b = _node("b", _MOUNT, is_directory=True)
        self.x = _node("x", "a")
        await self.mirror.apply_observations([_obs(self.a), _obs(self.b), _obs(self.x)])
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=mounts_of(self.mounts), root_ids={}),
            store=SynologyStore(
                mirror=self.mirror, ns=self.mirror.namespace("synology")
            ),
        )

    async def _rehearse_get_x(self):
        return await self.drive_api.get_file(SynologyPermanentLink("x"))

    async def commit(self) -> list[NodeId]:
        """Run a pass over the mount whose listings reported only a and b."""
        async with reconcile_session(
            {_MOUNT},
            mirror=self.mirror,
            node_sync=self.node_sync,
            locate=partial(self.syno_paths.locate, self.drive_api),
        ) as session:
            session.saw([self.a.id, self.b.id])
            removed = await session.commit_removals()
        await self.drain()
        return removed

    async def test_node_moved_during_pass_is_kept_under_new_parent(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_return(
            _syno_item("x", parent_id="b")
        )

        # when / then
        self.assertEqual(await self.commit(), [])

        # then
        stored = await self.mirror.get_node_by_id(self.x.id)
        assert stored is not None
        self.assertEqual(stored.parent_id, self.b.id)

    async def test_missing_node_is_removed(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_raise(
            SynologyNotFoundError("gone", error_code=1003)
        )

        # when / then
        self.assertEqual(await self.commit(), [self.x.id])

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(self.x.id))

    async def test_not_found_error_is_removal(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_raise(
            SynologyDirectoryNotFoundError("gone", error_code=1005)
        )

        # when / then
        self.assertEqual(await self.commit(), [self.x.id])

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(self.x.id))

    async def test_other_api_error_preserves_node(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_raise(
            SynologyApiError("unknown", error_code=1000)
        )

        # when / then
        self.assertEqual(await self.commit(), [])

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(self.x.id))

    async def test_node_moved_outside_mounts_is_removed(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_return(
            _syno_item("x", parent_id="elsewhere")
        )

        # when
        await self.commit()

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(self.x.id))

    async def test_network_failure_preserves_node(self) -> None:
        # given
        self.decoy.when(await self._rehearse_get_x()).then_raise(
            SynologyNetworkError("down")
        )

        # when / then
        self.assertEqual(await self.commit(), [])

        # then
        self.assertIsNotNone(await self.mirror.get_node_by_id(self.x.id))

    async def test_preserved_subtree_is_not_looked_up(self) -> None:
        # given
        async with reconcile_session(
            {_MOUNT},
            mirror=self.mirror,
            node_sync=self.node_sync,
            locate=partial(self.syno_paths.locate, self.drive_api),
        ) as session:
            # when
            session.saw([self.a.id, self.b.id])
            session.preserve(self.a.id)
            # then
            self.assertEqual(await session.commit_removals(), [])
        await self.drain()

        # then
        self.decoy.verify(await self.drive_api.get_file(matchers.Anything()), times=0)
        self.assertIsNotNone(await self.mirror.get_node_by_id(self.x.id))


class TestBackfillRemovalConfirmation(_RaceTestCase):
    async def test_node_moved_between_listings_is_kept(self) -> None:
        # given
        a = _node("a", _MOUNT, is_directory=True)
        b = _node("b", _MOUNT, is_directory=True)
        x = _node("x", "a")
        await self.mirror.apply_observations([_obs(a), _obs(b), _obs(x)])
        syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=mounts_of(self.mounts), root_ids={}),
            store=SynologyStore(
                mirror=self.mirror, ns=self.mirror.namespace("synology")
            ),
        )
        drive_api = self.decoy.mock(cls=SynologyClient)
        # x moved from a to b after b was listed and before a was.
        self.decoy.when(
            await drive_api.get_file(SynologyPermanentLink("x"))
        ).then_return(_syno_item("x", parent_id="b"))
        backfill = BackfillService(
            drive_api=drive_api,
            mirror=self.mirror,
            store=SynologyStore(
                mirror=self.mirror, ns=self.mirror.namespace("synology")
            ),
            syno_paths=syno_paths,
            node_sync=self.node_sync,
        )

        async def list_children(_api: object, parent_id: str) -> list[object]:
            if parent_id == _MOUNT:
                return [_syno_item("a", is_dir=True), _syno_item("b", is_dir=True)]
            return []

        # when
        with patch.object(syno_paths, "list_children", side_effect=list_children):
            stats = await backfill._reconcile_subtree(_MOUNT)
        await self.drain()

        # then
        self.assertEqual(stats["removed"], 0)
        stored = await self.mirror.get_node_by_id(x.id)
        assert stored is not None
        self.assertEqual(stored.parent_id, b.id)


class TestMediaFailures(_RaceTestCase):
    """Only a file never probed successfully is withheld, with a failure record."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.old = replace(_node("x", _MOUNT), is_image=True, width=10, height=10)
        await self.mirror.apply_observations([_obs(self.old)])
        self.fresh = replace(_node("y", _MOUNT), is_image=True)
        self.cursor = await self.mirror.get_cursor()

    def fail_enrichment(self, failing: bool = True) -> None:
        async def enrich(observation: NodeObservation, **_: object) -> NodeRecord:
            record = observation.record
            if failing:
                raise MediaEnrichmentError(record, ProbeFailure.INVALID, "unreadable")
            return replace(record, width=20, height=20)

        patcher = patch.object(self.node_sync._enricher, "enrich", enrich)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def failures(self) -> list[str]:
        return [f.source_key for f in await self.mirror.get_media_failures("synology")]

    async def test_rename_keeps_dimensions_without_probing(self) -> None:
        # given
        self.fail_enrichment()
        renamed = replace(self.old, name="renamed", width=0, height=0)

        # when
        await self.observed(lambda r: self.node_sync.upsert(_obs(renamed), read=r))
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(self.old.id)
        assert stored is not None
        self.assertEqual((stored.name, stored.width), ("renamed", 10))
        self.assertEqual(await self.failures(), [])

    async def test_failed_probe_of_changed_file_keeps_last_dimensions(self) -> None:
        # given
        self.fail_enrichment()
        changed = replace(self.old, size=5, hash="def", width=0, height=0)

        # when
        await self.observed(lambda r: self.node_sync.upsert(_obs(changed), read=r))
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(self.old.id)
        assert stored is not None
        self.assertEqual((stored.size, stored.hash, stored.width), (5, "def", 10))
        self.assertEqual(await self.failures(), [])

    async def test_failed_new_file_is_withheld_and_recorded(self) -> None:
        # given
        self.fail_enrichment()

        # when
        await self.observed(lambda r: self.node_sync.upsert(_obs(self.fresh), read=r))
        await self.drain()

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(self.fresh.id))
        self.assertEqual(await self.mirror.get_cursor(), self.cursor)
        self.assertEqual(await self.failures(), [self.fresh.id])

    async def test_failure_records_its_kind(self) -> None:
        # given
        self.fail_enrichment()

        # when
        await self.observed(lambda r: self.node_sync.upsert(_obs(self.fresh), read=r))
        await self.drain()

        # then
        (failure,) = await self.mirror.get_media_failures("synology")
        self.assertEqual(failure.kind, ProbeFailure.INVALID)

    async def test_transient_failure_is_retried_with_backoff(self) -> None:
        # given: the first probe fails transiently, the retry succeeds
        attempts: list[str] = []

        async def enrich(observation: NodeObservation, **_: object) -> NodeRecord:
            record = observation.record
            attempts.append(record.id)
            if len(attempts) == 1:
                raise MediaEnrichmentError(record, ProbeFailure.TRANSIENT, "reset")
            return replace(record, width=20, height=20)

        self.node_sync._retries = MediaRetries(first_delay=0.01)
        published = asyncio.Event()

        async def locate(_source_key: str) -> NodeObservation:
            published.set()
            return _obs(self.fresh)

        with patch.object(self.node_sync._enricher, "enrich", enrich):
            async with self.node_sync.retrying(locate):
                # when
                await self.observed(
                    lambda r: self.node_sync.upsert(_obs(self.fresh), read=r)
                )
                await self.drain()
                async with asyncio.timeout(5):
                    await published.wait()
                await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(self.fresh.id)
        assert stored is not None
        self.assertEqual(stored.width, 20)
        self.assertEqual(attempts, [self.fresh.id, self.fresh.id])
        self.assertEqual(await self.failures(), [])

    async def test_invalid_failure_is_not_retried(self) -> None:
        # given
        self.fail_enrichment()

        async def locate(_source_key: str) -> NodeObservation:
            raise AssertionError("must not retry")

        async with self.node_sync.retrying(locate):
            # when
            await self.observed(
                lambda r: self.node_sync.upsert(_obs(self.fresh), read=r)
            )
            await self.drain()

            # then
            self.assertEqual(self.node_sync._retries.pending("synology"), set())

    async def test_retry_publishes_and_clears_the_record(self) -> None:
        # given
        await self.mirror.mark_media_failure(
            "synology", self.fresh.id, ProbeFailure.INVALID
        )
        self.fail_enrichment(failing=False)

        async def locate(_source_key: str) -> NodeObservation:
            return _obs(self.fresh)

        # when
        await self.node_sync.retry_media_failures(locate=locate)
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(self.fresh.id)
        assert stored is not None
        self.assertEqual(stored.width, 20)
        self.assertEqual(await self.failures(), [])

    async def test_startup_retry_recovers_a_limit_failure(self) -> None:
        # given: a record left by the retired byte budget
        await self.mirror.mark_media_failure(
            "synology", self.fresh.id, ProbeFailure.LIMIT
        )
        self.fail_enrichment(failing=False)

        async def locate(_source_key: str) -> NodeObservation:
            return _obs(self.fresh)

        # when
        await self.node_sync.retry_media_failures(locate=locate)
        await self.drain()

        # then
        stored = await self.mirror.get_node_by_id(self.fresh.id)
        assert stored is not None
        self.assertEqual(stored.width, 20)
        self.assertEqual(await self.failures(), [])

    async def test_retry_of_missing_file_drops_the_record(self) -> None:
        # given
        await self.mirror.mark_media_failure(
            "synology", self.fresh.id, ProbeFailure.INVALID
        )

        async def locate(_source_key: str) -> None:
            return None

        # when
        await self.node_sync.retry_media_failures(locate=locate)
        await self.drain()

        # then
        self.assertEqual(await self.failures(), [])

    async def test_undecided_retry_keeps_the_record(self) -> None:
        # given
        await self.mirror.mark_media_failure(
            "synology", self.fresh.id, ProbeFailure.INVALID
        )

        async def locate(_source_key: str) -> None:
            raise LocateUndecidedError("down")

        # when
        await self.node_sync.retry_media_failures(locate=locate)
        await self.drain()

        # then
        self.assertEqual(await self.failures(), [self.fresh.id])

    async def test_removal_clears_the_record_of_a_withheld_file(self) -> None:
        # given: the permanent link maps to a public ID, as conversion mints it
        self.fail_enrichment()
        await self.mirror.write(
            lambda con: con.execute(
                "INSERT INTO node_ids (id, instance, native) VALUES (?, ?, ?)",
                (
                    self.fresh.id,
                    self.mirror.namespace("synology").key,
                    self.fresh.id,
                ),
            )
        )
        await self.observed(lambda r: self.node_sync.upsert(_obs(self.fresh), read=r))
        await self.drain()

        # when: a webhook reports the file removed
        await self.observed(lambda r: self.node_sync.delete(self.fresh.id, read=r))
        await self.drain()

        # then
        self.assertEqual(await self.failures(), [])

    async def test_older_success_does_not_apply_over_newer_failure(self) -> None:
        # given: an older read probes successfully but stalls; the content
        # changes, and a newer read finds it invalid
        gate = _Gate(lambda r: r.size == 1)
        older = replace(self.fresh, size=1)
        newer = replace(self.fresh, size=2)

        async def enrich(observation: NodeObservation, **_: object) -> NodeRecord:
            record = await gate.enrich(observation)
            if record.size == 2:
                raise MediaEnrichmentError(record, ProbeFailure.INVALID, "garbage")
            return replace(record, width=20, height=20)

        # when
        with patch.object(self.node_sync._enricher, "enrich", enrich):
            await self.run_gated(
                gate,
                self.observed(lambda r: self.node_sync.upsert(_obs(older), read=r)),
                lambda: self.observed(
                    lambda r: self.node_sync.upsert(_obs(newer), read=r)
                ),
            )

        # then
        self.assertIsNone(await self.mirror.get_node_by_id(self.fresh.id))
        self.assertEqual(await self.failures(), [self.fresh.id])

    async def test_stale_failure_is_not_recorded_after_newer_apply(self) -> None:
        # given
        gate = self.gate(lambda r: r.name == "y")
        newer = replace(self.fresh, name="newer")

        async def fail_after_gate(
            observation: NodeObservation, **kw: object
        ) -> NodeRecord:
            record = await gate.enrich(observation)
            if record.name == "y":
                raise MediaEnrichmentError(record, ProbeFailure.INVALID, "unreadable")
            return replace(record, width=20, height=20)

        # when
        with patch.object(self.node_sync._enricher, "enrich", fail_after_gate):
            await self.run_gated(
                gate,
                self.observed(
                    lambda r: self.node_sync.upsert(_obs(self.fresh), read=r)
                ),
                lambda: self.observed(
                    lambda r: self.node_sync.upsert_batch([_obs(newer)], read=r)
                ),
            )
        await self.drain()

        # then
        self.assertEqual(await self.failures(), [])
        stored = await self.mirror.get_node_by_id(self.fresh.id)
        assert stored is not None
        self.assertEqual(stored.name, "newer")

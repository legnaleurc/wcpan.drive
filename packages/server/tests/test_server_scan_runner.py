"""The backend write scope and scan runner, over an in-memory source."""

import asyncio
import logging
import shutil
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._lib.write_queue import create_write_queue, write_worker
from wcpan.drive.server._services.ids import NodeIds
from wcpan.drive.server._services.media.enricher import MediaEnrichmentError
from wcpan.drive.server._services.media.probe.source import ProbeFailure
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.store import MirrorStore
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.sync.metadata import (
    create_metadata_queue,
    metadata_worker,
)
from wcpan.drive.server._services.sync.scan import compare_pass, run_pass
from wcpan.drive.server._services.sync.service import (
    NodeSyncService,
    WriterStoppedError,
)
from wcpan.drive.server.exceptions import ListingUnavailableError
from wcpan.drive.server.types import (
    Comparison,
    ComparisonReport,
    Contents,
    CoreServices,
    Listing,
    NodeObservation,
    ReadCoreServices,
    Scanner,
    ScanPlan,
)

from ._fake_backend import FakeSchema
from ._mounts import ROOT_ID, mount_id


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)

_TS = 1_700_000_000
_A = mount_id("a")
_B = mount_id("b")


def _id(name: str) -> NodeId:
    return NodeId(name)


def _node(
    name: str,
    parent_id: NodeId | None,
    *,
    is_directory: bool = False,
    modified_time: int = _TS,
) -> NodeRecord:
    return NodeRecord(
        id=_id(name),
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=_TS,
        modified_time=modified_time,
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


def _folder(name: str, parent_id: NodeId) -> NodeRecord:
    return _node(name, parent_id, is_directory=True)


def _obs(record: NodeRecord) -> NodeObservation:
    return NodeObservation(record=record, locator=None)


class _Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def pass_through(self) -> None:
        self.entered.set()
        await self.release.wait()


class _Enricher:
    """Stands in for media probing; selected records block or fail."""

    def __init__(self) -> None:
        self.gates: dict[NodeId, _Gate] = {}
        self.failing = set[NodeId]()
        self.probed: list[NodeId] = []

    async def enrich(
        self, observation: NodeObservation, *, force_refresh: bool = False
    ) -> NodeRecord:
        record = observation.record
        self.probed.append(record.id)
        gate = self.gates.get(record.id)
        if gate is not None:
            await gate.pass_through()
        if record.id in self.failing:
            raise MediaEnrichmentError(record, ProbeFailure.INVALID, "unreadable")
        return record


class _Source:
    """A tree of nodes by parent; folders are their node IDs."""

    def __init__(self, nodes: Iterable[NodeRecord] = ()) -> None:
        self.nodes = {n.id: n for n in nodes}

    def children(self, folder: NodeId) -> list[NodeRecord]:
        return [n for n in self.nodes.values() if n.parent_id == folder]

    def remove(self, node_id: NodeId) -> None:
        del self.nodes[node_id]


class _Scanner(Scanner[NodeId]):
    def __init__(
        self,
        source: _Source,
        roots: Iterable[NodeId] = (_A,),
        *,
        preserved: Iterable[NodeId] = (),
        log: list[str] | None = None,
    ) -> None:
        self.source = source
        self.roots = list(roots)
        self.preserved = list(preserved)
        self.log: list[str] = [] if log is None else log
        self.skips = set[NodeId]()
        self.keeps = set[NodeId]()
        self.failing = set[NodeId]()
        self.broken_handles = set[NodeId]()
        self.gates: dict[NodeId, _Gate] = {}
        self.prepare_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.finished_gate: _Gate | None = None
        self.on_list: Callable[[NodeId], Awaitable[None]] | None = None
        self.on_root_finished: Callable[[], Awaitable[None]] | None = None
        self.on_locate: Callable[[], Awaitable[None]] | None = None
        self.root_results: dict[NodeId, bool] = {}
        self.pass_result: bool | None = None

    async def prepare(self) -> ScanPlan[NodeId]:
        self.log.append("prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        return ScanPlan(
            scope={*self.roots, *self.preserved},
            roots=self.roots,
            preserved=self.preserved,
        )

    def folder_id(self, folder: NodeId) -> NodeId:
        if folder in self.broken_handles:
            raise ValueError(f"bad handle {folder}")
        return folder

    async def skip(self, folder: NodeId) -> bool:
        self.log.append(f"skip:{folder}")
        return folder in self.skips

    async def list_folder(self, folder: NodeId) -> Listing[NodeId]:
        self.log.append(f"list:{folder}")
        if self.on_list is not None:
            await self.on_list(folder)
        gate = self.gates.get(folder)
        if gate is not None:
            await gate.pass_through()
        if folder in self.failing:
            raise OSError(f"cannot list {folder}")
        children = self.source.children(folder)
        kept = [c.id for c in children if c.id in self.keeps]
        return Listing(
            seen=[c.id for c in children],
            changed=[_obs(c) for c in children],
            subfolders=[
                c.id for c in children if c.is_directory and c.id not in self.keeps
            ],
            keep=kept,
        )

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        self.log.append(f"locate:{node_id}")
        if self.on_locate is not None:
            await self.on_locate()
        record = self.source.nodes.get(node_id)
        return _obs(record) if record is not None else None

    async def root_finished(self, root: NodeId, complete: bool) -> None:
        self.log.append(f"root:{root}")
        self.root_results[root] = complete
        if self.on_root_finished is not None:
            await self.on_root_finished()

    async def pass_finished(self, complete: bool) -> None:
        self.log.append("finished")
        self.pass_result = complete
        if self.finished_gate is not None:
            await self.finished_gate.pass_through()
        if self.finish_error is not None:
            raise self.finish_error


class _Comparison(Comparison[NodeId]):
    def __init__(self, source: _Source, roots: Iterable[NodeId] = (_A,)) -> None:
        self.source = source
        self.roots = list(roots)
        self.failing = set[NodeId]()
        self.stale = set[NodeId]()
        self.compared: list[list[NodeId]] = []

    async def prepare(self) -> ScanPlan[NodeId]:
        return ScanPlan(scope=set(self.roots), roots=self.roots, preserved=[])

    def folder_id(self, folder: NodeId) -> NodeId:
        return folder

    async def list_folder(self, folder: NodeId) -> Contents[NodeId]:
        if folder in self.failing:
            raise OSError(f"cannot list {folder}")
        children = self.source.children(folder)
        return Contents(
            children=[_obs(c) for c in children],
            subfolders=[c.id for c in children if c.is_directory],
        )

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        record = self.source.nodes.get(node_id)
        return _obs(record) if record is not None else None

    async def differs(
        self, pairs: Sequence[tuple[NodeObservation, NodeRecord]]
    ) -> set[NodeId]:
        self.compared.append([o.record.id for o, _ in pairs])
        return {o.record.id for o, _ in pairs} & self.stale


def _report(
    *, checked: int = 0, added: int = 0, updated: int = 0, removed: int = 0
) -> ComparisonReport:
    return ComparisonReport(
        checked=checked, added=added, updated=updated, removed=removed, list_errors=0
    )


class _FlowTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wcpan_flow_"))
        self.pool = ThreadPoolExecutor(2)
        self.mirror = MirrorStore(
            str(self.tmp / "mirror.sqlite"),
            off_main=OffMainService(pool=self.pool),
            schemas={"fake": FakeSchema()},
        )
        await self.mirror.ensure_schema()
        await self.mirror.bulk_upsert_nodes(
            [
                _node(ROOT_ID, None, is_directory=True),
                _folder(_A, ROOT_ID),
                _folder(_B, ROOT_ID),
            ]
        )
        self.write_queue = create_write_queue()
        self.metadata_queue = create_metadata_queue()
        self.writer = asyncio.create_task(write_worker(self.write_queue))
        self.sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=self.write_queue,
            metadata_queue=self.metadata_queue,
            probing=MediaProbing(runner=None),
            writer=self.writer,
        ).for_backend("fake")
        self.enricher = _Enricher()
        enricher_patch = patch.object(self.sync, "_enricher", self.enricher)
        enricher_patch.start()
        self.addCleanup(enricher_patch.stop)
        self.worker = asyncio.create_task(
            metadata_worker(self.metadata_queue, self.sync.process_metadata_item)
        )
        self.core = CoreServices(
            mirror=self.mirror,
            node_sync=self.sync,
            ids=NodeIds(
                mirror=self.mirror,
                write_queue=self.write_queue,
                key=self.mirror.namespace("fake").key,
            ),
            media=MediaProbing(runner=None),
        )

    async def asyncTearDown(self) -> None:
        for task in (self.worker, self.writer):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self.pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.tmp)

    async def has(self, name: str) -> bool:
        return await self.mirror.get_node_by_id(_id(name)) is not None

    async def scan(self, scanner: _Scanner) -> None:
        async with asyncio.timeout(5):
            await self.core.scans.run(scanner)

    async def block_writes(self) -> _Gate:
        """Hold the write worker on a job until the gate is released."""
        gate = _Gate()
        await self.sync.enqueue(gate.pass_through)
        await gate.entered.wait()
        return gate

    async def stop_writer(self) -> None:
        async def fail() -> None:
            raise RuntimeError("disk failure")

        await self.sync.enqueue(fail)
        with suppress(RuntimeError):
            await self.writer

    async def settle(self) -> None:
        """Let tasks that are ready run until they next block."""
        for _ in range(20):
            await asyncio.sleep(0)


class ScanPassTest(_FlowTestCase):
    async def test_publishes_the_listed_tree(self) -> None:
        # given
        source = _Source(
            [_folder("d", _A), _node("f", _A), _node("g", _id("d"))],
        )
        scanner = _Scanner(source)
        # when
        await self.scan(scanner)
        # then
        for name in ("d", "f", "g"):
            self.assertTrue(await self.has(name), name)
        self.assertEqual(scanner.root_results, {_A: True})
        self.assertIs(scanner.pass_result, True)

    async def test_finish_hooks_run_after_roots_in_order(self) -> None:
        # given
        scanner = _Scanner(_Source(), roots=[_A, _B])
        # when
        await self.scan(scanner)
        # then
        self.assertEqual(scanner.log[-3:], [f"root:{_A}", f"root:{_B}", "finished"])

    async def test_node_gone_from_the_source_is_removed(self) -> None:
        # given
        source = _Source([_node("f", _A), _node("g", _A)])
        await self.scan(_Scanner(source))
        source.remove(_id("f"))
        # when
        await self.scan(_Scanner(source))
        # then
        self.assertFalse(await self.has("f"))
        self.assertTrue(await self.has("g"))

    async def test_unlisted_node_found_by_locate_is_kept(self) -> None:
        # given: moved under a folder this pass does not list
        source = _Source([_folder("d", _B), _node("f", _A)])
        await self.scan(_Scanner(source, roots=[_A, _B]))
        source.nodes[_id("f")] = _node("f", _id("d"))
        scanner = _Scanner(source, roots=[_A])
        # when
        await self.scan(scanner)
        # then
        record = await self.mirror.get_node_by_id(_id("f"))
        assert record is not None
        self.assertEqual(record.parent_id, _id("d"))
        self.assertIn("locate:f", scanner.log)

    async def test_failed_listing_keeps_subtree_and_marks_only_its_root(self) -> None:
        # given
        source = _Source(
            [_folder("d", _A), _node("g", _id("d")), _node("h", _B)],
        )
        await self.scan(_Scanner(source, roots=[_A, _B]))
        source.remove(_id("g"))
        source.remove(_id("h"))
        scanner = _Scanner(source, roots=[_A, _B])
        scanner.failing.add(_id("d"))
        # when
        await self.scan(scanner)
        # then
        self.assertTrue(await self.has("g"))
        self.assertFalse(await self.has("h"))
        self.assertEqual(scanner.root_results, {_A: False, _B: True})
        self.assertIs(scanner.pass_result, False)

    async def test_skipped_folder_is_preserved_without_being_incomplete(self) -> None:
        # given
        source = _Source([_folder("d", _A), _node("g", _id("d"))])
        await self.scan(_Scanner(source))
        source.remove(_id("g"))
        scanner = _Scanner(source)
        scanner.skips.add(_id("d"))
        # when
        await self.scan(scanner)
        # then
        self.assertTrue(await self.has("g"))
        self.assertNotIn("list:d", scanner.log)
        self.assertEqual(scanner.root_results, {_A: True})

    async def test_skip_that_raises_counts_as_a_failed_listing(self) -> None:
        # given
        source = _Source([_folder("d", _A), _node("g", _id("d"))])
        await self.scan(_Scanner(source))
        source.remove(_id("g"))
        scanner = _Scanner(source)

        async def skip(folder: NodeId) -> bool:
            raise OSError("cannot check")

        # when
        with patch.object(scanner, "skip", skip):
            await self.scan(scanner)
        # then
        self.assertTrue(await self.has("g"))
        self.assertEqual(scanner.root_results, {_A: False})

    async def test_kept_folder_is_preserved_and_not_visited(self) -> None:
        # given
        source = _Source([_folder("d", _A), _node("g", _id("d"))])
        await self.scan(_Scanner(source))
        source.remove(_id("g"))
        scanner = _Scanner(source)
        scanner.keeps.add(_id("d"))
        # when
        await self.scan(scanner)
        # then
        self.assertTrue(await self.has("g"))
        self.assertNotIn("list:d", scanner.log)
        self.assertIs(scanner.pass_result, True)

    async def test_preserved_scope_makes_the_pass_incomplete(self) -> None:
        # given
        source = _Source([_node("f", _A), _node("g", _B)])
        await self.scan(_Scanner(source, roots=[_A, _B]))
        source.remove(_id("g"))
        scanner = _Scanner(source, roots=[_A], preserved=[_B])
        # when
        await self.scan(scanner)
        # then
        self.assertTrue(await self.has("g"))
        self.assertEqual(scanner.root_results, {_A: True})
        self.assertIs(scanner.pass_result, False)

    async def test_roots_run_one_at_a_time_when_not_concurrent(self) -> None:
        # given
        source = _Source([_folder("d", _A)])
        scanner = _Scanner(source, roots=[_A, _B])
        scanner.concurrent_roots = False
        gate = _Gate()
        scanner.gates[_id("d")] = gate
        task = asyncio.create_task(self.scan(scanner))
        # when
        await gate.entered.wait()
        await self.settle()
        listed_during = list(scanner.log)
        gate.release.set()
        await task
        # then
        self.assertNotIn(f"list:{_B}", listed_during)
        self.assertIn(f"list:{_B}", scanner.log)

    async def test_finish_hooks_see_enriched_files(self) -> None:
        # given
        gate = _Gate()
        self.enricher.gates[_id("f")] = gate
        scanner = _Scanner(_Source([_node("f", _A)]))
        seen: list[bool] = []

        async def on_root_finished() -> None:
            seen.append(await self.has("f"))

        scanner.on_root_finished = on_root_finished
        task = asyncio.create_task(self.scan(scanner))
        # when
        await gate.entered.wait()
        await self.settle()
        finished_early = "finished" in scanner.log
        gate.release.set()
        await task
        # then
        self.assertFalse(finished_early)
        self.assertEqual(seen, [True])

    async def test_finish_hooks_wait_for_confirmed_removals(self) -> None:
        # given: the removal queues behind a blocked write
        source = _Source([_node("f", _A)])
        await self.scan(_Scanner(source))
        source.remove(_id("f"))
        gate = _Gate()
        scanner = _Scanner(source)

        async def on_locate() -> None:
            await self.sync.enqueue(gate.pass_through)

        scanner.on_locate = on_locate
        seen: list[bool] = []

        async def on_root_finished() -> None:
            seen.append(await self.has("f"))

        scanner.on_root_finished = on_root_finished
        task = asyncio.create_task(self.scan(scanner))
        # when
        await gate.entered.wait()
        await self.settle()
        gate.release.set()
        await task
        # then
        self.assertEqual(seen, [False])


class ScanLoggingTest(_FlowTestCase):
    _LOGGER = "wcpan.drive.server._services.sync.scan"

    async def test_source_error_is_one_warning_line(self) -> None:
        # given
        scanner = _Scanner(_Source())
        scanner.failing.add(_A)
        # when
        with self.assertLogs(self._LOGGER, "WARNING") as logs:
            await self.scan(scanner)
        # then
        (record,) = logs.records
        self.assertEqual(record.levelname, "WARNING")
        self.assertIsNone(record.exc_info)
        self.assertIn("cannot list", record.getMessage())

    async def test_unavailable_listing_keeps_the_subtree(self) -> None:
        # given
        source = _Source([_folder("d", _A), _node("f", _id("d"))])
        await self.scan(_Scanner(source))
        source.remove(_id("f"))
        scanner = _Scanner(source)

        async def list_folder(folder: NodeId) -> Listing[NodeId]:
            raise ListingUnavailableError("NAS busy")

        # when
        with (
            patch.object(scanner, "list_folder", list_folder),
            self.assertLogs(self._LOGGER, "WARNING") as logs,
        ):
            await self.scan(scanner)
        # then
        (record,) = logs.records
        self.assertIsNone(record.exc_info)
        self.assertEqual(scanner.root_results, {_A: False})
        self.assertTrue(await self.has("f"))

    async def test_unexpected_error_fails_the_pass(self) -> None:
        # given
        source = _Source([_node("f", _A)])
        await self.scan(_Scanner(source))
        source.remove(_id("f"))
        scanner = _Scanner(source)

        async def list_folder(folder: NodeId) -> Listing[NodeId]:
            raise KeyError("type")

        # when
        with patch.object(scanner, "list_folder", list_folder):
            with self.assertRaises(KeyError):
                await self.scan(scanner)
        # then: nothing was removed, and no finish hook ran
        self.assertTrue(await self.has("f"))
        self.assertIsNone(scanner.pass_result)


class ScanFailureTest(_FlowTestCase):
    async def test_failed_prepare_reconciles_nothing(self) -> None:
        # given
        source = _Source([_node("f", _A)])
        await self.scan(_Scanner(source))
        source.remove(_id("f"))
        scanner = _Scanner(source)
        scanner.prepare_error = ValueError("no mount")
        # when
        with self.assertRaisesRegex(ValueError, "no mount"):
            await self.scan(scanner)
        # then
        self.assertTrue(await self.has("f"))
        await self.scan(_Scanner(source))
        self.assertFalse(await self.has("f"))

    async def test_failed_finish_hook_fails_the_pass_and_frees_the_lock(self) -> None:
        # given
        scanner = _Scanner(_Source())
        scanner.finish_error = ValueError("no checkpoint")
        # when
        with self.assertRaisesRegex(ValueError, "no checkpoint"):
            await self.scan(scanner)
        # then
        await self.scan(_Scanner(_Source()))

    async def test_failed_pass_settles_submitted_work_before_unlocking(self) -> None:
        # given: a file is being enriched when traversal fails
        gate = _Gate()
        self.enricher.gates[_id("f")] = gate
        log: list[str] = []
        source = _Source([_node("f", _A), _folder("d", _A)])
        failing = _Scanner(source, log=log)
        failing.broken_handles.add(_id("d"))
        failed = asyncio.create_task(self.scan(failing))
        await gate.entered.wait()
        following = asyncio.create_task(self.scan(_Scanner(source, log=log)))
        await self.settle()
        # when
        log.append("released")
        gate.release.set()
        with self.assertRaisesRegex(ValueError, "bad handle"):
            await failed
        await following
        # then
        self.assertLess(log.index("released"), log.index("prepare", 1))
        self.assertTrue(await self.has("f"))

    async def test_stopped_writer_fails_the_pass_promptly(self) -> None:
        # given
        await self.stop_writer()
        # when, then
        with self.assertRaises(WriterStoppedError):
            await self.scan(_Scanner(_Source([_folder("d", _A), _node("f", _A)])))


class ScanOrderingTest(_FlowTestCase):
    async def test_passes_including_finish_hooks_do_not_overlap(self) -> None:
        # given
        log: list[str] = []
        first = _Scanner(_Source(), log=log)
        first.finished_gate = _Gate()
        running = asyncio.create_task(self.scan(first))
        await first.finished_gate.entered.wait()
        second = asyncio.create_task(self.scan(_Scanner(_Source(), log=log)))
        await self.settle()
        # when
        log.append("released")
        first.finished_gate.release.set()
        await asyncio.gather(running, second)
        # then
        self.assertLess(log.index("released"), log.index("prepare", 1))

    async def test_stale_listing_does_not_overwrite_a_newer_write(self) -> None:
        # given: the listing's read starts before the newer write's
        old = _node("f", _A, modified_time=_TS)
        new = _node("f", _A, modified_time=_TS + 1)
        gate = _Gate()
        scanner = _Scanner(_Source([old]))
        scanner.gates[_A] = gate
        task = asyncio.create_task(self.scan(scanner))
        await gate.entered.wait()
        async with self.core.observe() as changes:
            await changes.upserted(_obs(new))
            await changes.landed()
        # when
        gate.release.set()
        await task
        # then
        record = await self.mirror.get_node_by_id(_id("f"))
        assert record is not None
        self.assertEqual(record.modified_time, _TS + 1)


class WalkTest(_FlowTestCase):
    async def walk(self, scanner: _Scanner, root: NodeId) -> None:
        async with asyncio.timeout(5):
            await self.core.scans.walk(scanner, root)

    async def test_writes_every_child_without_skip_or_removal(self) -> None:
        # given
        source = _Source([_node("old", _A)])
        await self.scan(_Scanner(source))
        source.remove(_id("old"))
        source.nodes.update(
            {n.id: n for n in (_folder("d", _A), _node("g", _id("d")))},
        )
        scanner = _Scanner(source)
        scanner.skips.add(_id("d"))
        # when
        await self.walk(scanner, _A)
        await self.sync.wait_enrichment_drained()
        # then
        self.assertTrue(await self.has("d"))
        self.assertTrue(await self.has("g"))
        self.assertTrue(await self.has("old"))
        self.assertFalse([e for e in scanner.log if e.startswith("skip:")])
        self.assertIsNone(scanner.pass_result)

    async def test_directories_land_before_descending(self) -> None:
        # given
        scanner = _Scanner(_Source([_folder("d", _A)]))
        visible: list[bool] = []

        async def on_list(folder: NodeId) -> None:
            if folder == _id("d"):
                visible.append(await self.has("d"))

        scanner.on_list = on_list
        # when
        await self.walk(scanner, _A)
        # then
        self.assertEqual(visible, [True])

    async def test_failed_listing_raises(self) -> None:
        # given
        scanner = _Scanner(_Source([_folder("d", _A)]))
        scanner.failing.add(_id("d"))
        # when, then
        with self.assertRaisesRegex(OSError, "cannot list d"):
            await self.walk(scanner, _A)

    async def test_does_not_need_the_pass_lock(self) -> None:
        # given
        scanner = _Scanner(_Source([_node("f", _A)]))
        # when
        async with self.core.observe(exclusive=True):
            await self.walk(scanner, _A)
        await self.sync.wait_enrichment_drained()
        # then
        self.assertTrue(await self.has("f"))


class CompareTest(_FlowTestCase):
    async def compare(
        self, comparison: _Comparison, *, dry_run: bool = False
    ) -> ComparisonReport:
        async with asyncio.timeout(5):
            return await self.core.compare(comparison, dry_run=dry_run)

    async def seed(self, *nodes: NodeRecord) -> None:
        await self.mirror.bulk_upsert_nodes(list(nodes))

    async def test_writes_what_differs_and_removes_what_is_gone(self) -> None:
        # given
        same = _node("same", _A)
        changed = _node("changed", _A)
        await self.seed(same, changed, _node("gone", _A))
        source = _Source(
            [same, replace(changed, modified_time=_TS + 1), _node("new", _A)]
        )
        # when
        report = await self.compare(_Comparison(source))
        # then
        self.assertEqual(report, _report(checked=3, added=1, updated=1, removed=1))
        record = await self.mirror.get_node_by_id(_id("changed"))
        assert record is not None
        self.assertEqual(record.modified_time, _TS + 1)
        self.assertTrue(await self.has("new"))
        self.assertFalse(await self.has("gone"))

    async def test_descends_into_every_listed_folder(self) -> None:
        # given
        source = _Source([_folder("d", _A), _node("f", _id("d"))])
        # when
        report = await self.compare(_Comparison(source))
        # then
        self.assertEqual(report, _report(checked=2, added=2))
        self.assertTrue(await self.has("f"))

    async def test_dry_run_reports_without_writing(self) -> None:
        # given
        changed = _node("changed", _A)
        await self.seed(changed, _node("gone", _A))
        source = _Source([replace(changed, name="renamed"), _node("new", _A)])
        # when
        report = await self.compare(_Comparison(source), dry_run=True)
        await self.sync.wait_enrichment_drained()
        # then
        self.assertEqual(report, _report(checked=2, added=1, updated=1, removed=1))
        self.assertFalse(await self.has("new"))
        self.assertTrue(await self.has("gone"))
        record = await self.mirror.get_node_by_id(_id("changed"))
        assert record is not None
        self.assertEqual(record.name, "changed")

    async def test_backend_state_the_record_does_not_show_is_updated(self) -> None:
        # given
        node = _node("f", _A)
        await self.seed(node)
        comparison = _Comparison(_Source([node, _node("new", _A)]))
        comparison.stale.add(_id("f"))
        # when
        report = await self.compare(comparison)
        # then: only mirrored children are offered to the hook
        self.assertEqual(report, _report(checked=2, added=1, updated=1))
        self.assertEqual(comparison.compared, [[_id("f")]])

    async def test_only_media_without_dimensions_is_probed_again(self) -> None:
        # given: one image published without dimensions, one with them
        image = replace(_node("zero", _A), is_image=True, mime_type="image/png")
        done = replace(image, id=_id("done"), name="done", width=8, height=6)
        await self.seed(image, done)
        listed = [replace(r, width=0, height=0) for r in (image, done)]
        # when
        report = await self.compare(_Comparison(_Source(listed)))
        # then
        self.assertEqual(report.updated, 1)
        self.assertEqual(self.enricher.probed, [_id("zero")])
        record = await self.mirror.get_node_by_id(_id("done"))
        assert record is not None
        self.assertEqual((record.width, record.height), (8, 6))

    async def test_failed_listing_keeps_its_subtree(self) -> None:
        # given
        await self.seed(_folder("d", _A), _node("f", _id("d")))
        comparison = _Comparison(_Source([_folder("d", _A)]))
        comparison.failing.add(_id("d"))
        # when
        report = await self.compare(comparison)
        # then
        self.assertEqual(report.list_errors, 1)
        self.assertEqual(report.removed, 0)
        self.assertTrue(await self.has("f"))

    async def test_unexpected_listing_error_fails_the_pass(self) -> None:
        # given
        await self.seed(_node("f", _A))
        comparison = _Comparison(_Source())

        async def list_folder(folder: NodeId) -> Contents[NodeId]:
            raise KeyError("type")

        # when
        with patch.object(comparison, "list_folder", list_folder):
            with self.assertRaises(KeyError):
                await self.compare(comparison)
        # then
        self.assertTrue(await self.has("f"))

    async def test_failure_after_the_listing_fails_the_pass(self) -> None:
        # given
        node = _node("f", _A)
        await self.seed(node)
        comparison = _Comparison(_Source([node]))

        async def differs(
            pairs: Sequence[tuple[NodeObservation, NodeRecord]],
        ) -> set[NodeId]:
            raise OSError("store unreadable")

        # when, then
        with patch.object(comparison, "differs", differs):
            with self.assertRaisesRegex(RuntimeError, "comparing folder"):
                await self.compare(comparison)

    async def test_dry_run_calls_no_finish_hooks(self) -> None:
        # given
        scanner = _Scanner(_Source([_node("f", _A)]))
        plan = await scanner.prepare()
        # when
        await run_pass(scanner, plan, mirror=self.mirror, writer=None)
        # then
        self.assertEqual(scanner.root_results, {})
        self.assertIsNone(scanner.pass_result)

    async def test_returns_after_its_writes_land(self) -> None:
        # given
        await self.seed(_node("gone", _A))
        # when
        await self.compare(_Comparison(_Source([_node("new", _A)])))
        # then: no drain needed to see the result
        self.assertTrue(await self.has("new"))
        self.assertFalse(await self.has("gone"))

    async def test_waits_for_a_running_pass(self) -> None:
        # given
        scanner = _Scanner(_Source())
        scanner.finished_gate = _Gate()
        scan = asyncio.create_task(self.scan(scanner))
        await scanner.finished_gate.entered.wait()
        # when
        compare = asyncio.create_task(self.compare(_Comparison(_Source())))
        await self.settle()
        # then
        self.assertFalse(compare.done())
        scanner.finished_gate.release.set()
        await scan
        await compare

    async def test_dry_run_needs_no_writer(self) -> None:
        # given
        await self.seed(_node("gone", _A))
        comparison = _Comparison(_Source([_node("new", _A)]))
        # when
        report = await compare_pass(comparison, mirror=self.mirror, writer=None)
        # then
        self.assertEqual(report, _report(checked=1, added=1, removed=1))
        self.assertFalse(await self.has("new"))
        self.assertTrue(await self.has("gone"))

    async def test_read_only_core_refuses_a_real_run(self) -> None:
        # given
        core = ReadCoreServices(
            mirror=self.mirror,
            ids=MagicMock(),
            media=MediaProbing(runner=None),
            schemas=MagicMock(),
        )
        comparison = _Comparison(_Source([_node("new", _A)]))
        # when
        report = await core.compare(comparison)
        # then
        self.assertEqual(report, _report(checked=1, added=1))
        with self.assertRaisesRegex(RuntimeError, "dry run"):
            await core.compare(comparison, dry_run=False)


class ObserveTest(_FlowTestCase):
    async def test_upserted_returns_the_record_and_landed_makes_it_visible(
        self,
    ) -> None:
        # given
        record = _node("f", _A)
        # when
        async with self.core.observe() as changes:
            result = await changes.upserted(_obs(record))
            await changes.landed()
            visible = await self.has("f")
        # then
        self.assertEqual(result, record)
        self.assertTrue(visible)

    async def test_removed_deletes_the_node(self) -> None:
        # given
        async with self.core.observe() as changes:
            await changes.upserted(_obs(_node("f", _A)))
            await changes.landed()
        # when
        async with self.core.observe() as changes:
            await changes.removed(_id("f"))
            await changes.landed()
        # then
        self.assertFalse(await self.has("f"))

    async def test_read_order_comes_from_block_entry(self) -> None:
        # given: the older block writes last
        older = self.core.observe()
        older_changes = await older.__aenter__()
        async with self.core.observe() as newer:
            await newer.upserted(_obs(_node("f", _A, modified_time=_TS + 1)))
            await newer.landed()
        # when
        await older_changes.upserted(_obs(_node("f", _A, modified_time=_TS)))
        await older_changes.landed()
        await older.__aexit__(None, None, None)
        # then
        record = await self.mirror.get_node_by_id(_id("f"))
        assert record is not None
        self.assertEqual(record.modified_time, _TS + 1)

    async def test_strict_probe_failure_raises_inside_the_block(self) -> None:
        # given
        self.enricher.failing.add(_id("f"))
        reached_after = False
        # when
        with self.assertRaises(MediaEnrichmentError):
            async with self.core.observe() as changes:
                await changes.upserted(_obs(_node("f", _A)), strict=True)
                reached_after = True
        # then
        self.assertFalse(reached_after)

    async def test_plain_block_does_not_wait_for_writes(self) -> None:
        # given
        gate = await self.block_writes()
        # when
        async with asyncio.timeout(5):
            async with self.core.observe() as changes:
                await changes.upserted(_obs(_node("f", _A)))
        visible = await self.has("f")
        gate.release.set()
        await self.sync.barrier()
        # then
        self.assertFalse(visible)
        self.assertTrue(await self.has("f"))


class ExclusiveObserveTest(_FlowTestCase):
    async def test_writes_land_before_a_scan_can_start(self) -> None:
        # given
        log: list[str] = []
        gate = await self.block_writes()

        async def mutate() -> None:
            async with self.core.observe(exclusive=True) as changes:
                await changes.upserted(_obs(_node("f", _A)))

        mutation = asyncio.create_task(mutate())
        await self.settle()
        source = _Source([_node("f", _A)])
        scan = asyncio.create_task(self.scan(_Scanner(source, log=log)))
        await self.settle()
        # when
        log.append("released")
        gate.release.set()
        await asyncio.gather(mutation, scan)
        # then
        self.assertEqual(log[:2], ["released", "prepare"])

    async def test_waits_for_a_running_pass(self) -> None:
        # given
        scanner = _Scanner(_Source())
        scanner.finished_gate = _Gate()
        running = asyncio.create_task(self.scan(scanner))
        await scanner.finished_gate.entered.wait()
        entered = asyncio.Event()

        async def mutate() -> None:
            async with self.core.observe(exclusive=True):
                entered.set()

        mutation = asyncio.create_task(mutate())
        await self.settle()
        # when
        entered_during_pass = entered.is_set()
        scanner.finished_gate.release.set()
        await asyncio.gather(running, mutation)
        # then
        self.assertFalse(entered_during_pass)
        self.assertTrue(entered.is_set())

    async def test_block_error_still_waits_for_queued_writes(self) -> None:
        # given
        with self.assertRaisesRegex(ValueError, "boom"):
            async with self.core.observe(exclusive=True) as changes:
                await changes.upserted(_obs(_node("f", _A)))
                raise ValueError("boom")
        # then
        self.assertTrue(await self.has("f"))
        await self.scan(_Scanner(_Source([_node("f", _A)])))

    async def test_stopped_writer_fails_exit_and_frees_the_lock(self) -> None:
        # given
        await self.stop_writer()
        # when
        with self.assertRaises(WriterStoppedError):
            async with asyncio.timeout(5):
                async with self.core.observe(exclusive=True):
                    pass
        # then: a pass can take the lock, and fails in its own first step
        scanner = _Scanner(_Source())
        scanner.prepare_error = LookupError("reached")
        with self.assertRaisesRegex(LookupError, "reached"):
            await self.scan(scanner)

    async def test_block_error_wins_over_a_stopped_writer(self) -> None:
        # given
        await self.stop_writer()
        # when
        with self.assertRaisesRegex(ValueError, "boom") as caught:
            async with asyncio.timeout(5):
                async with self.core.observe(exclusive=True):
                    raise ValueError("boom")
        # then
        self.assertIsInstance(caught.exception.__cause__, WriterStoppedError)


def _record_state(con: sqlite3.Connection, value: str) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS state (value TEXT)")
    con.execute("INSERT INTO state VALUES (?)", (value,))


class BackendStateTest(_FlowTestCase):
    def state(self) -> list[str]:
        con = sqlite3.connect(self.tmp / "mirror.sqlite")
        try:
            return [row[0] for row in con.execute("SELECT value FROM state")]
        finally:
            con.close()

    async def test_state_write_lands_after_earlier_writes(self) -> None:
        # given: a node write is queued behind a blocked job
        gate = await self.block_writes()
        async with self.core.observe() as changes:
            await changes.upserted(_obs(_node("f", _A)))
        seen: list[bool] = []
        has = self.mirror.get_node_by_id

        async def checked() -> None:
            seen.append(await has(_id("f")) is not None)

        # when
        await self.core.enqueue_write(_record_state, "checkpoint")
        await self.sync.enqueue(checked)
        gate.release.set()
        async with asyncio.timeout(5):
            await self.core.writes_landed()
        # then
        self.assertEqual(self.state(), ["checkpoint"])
        self.assertEqual(seen, [True])

    async def test_writes_landed_waits_for_a_queued_state_write(self) -> None:
        # given
        gate = await self.block_writes()
        await self.core.enqueue_write(_record_state, "checkpoint")
        waiting = asyncio.create_task(self.core.writes_landed())
        await self.settle()
        # when
        finished_early = waiting.done()
        gate.release.set()
        async with asyncio.timeout(5):
            await waiting
        # then
        self.assertFalse(finished_early)
        self.assertEqual(self.state(), ["checkpoint"])


class EnrichmentTest(_FlowTestCase):
    async def test_drain_waits_for_queued_enrichment(self) -> None:
        # given
        gate = _Gate()
        self.enricher.gates[_id("f")] = gate
        async with self.sync.observe() as read:
            await self.sync.upsert_file_batch([_obs(_node("f", _A))], read=read)
        await gate.entered.wait()
        draining = asyncio.create_task(self.core.enrichment.drain())
        await self.settle()
        # when
        finished_early = draining.done()
        gate.release.set()
        async with asyncio.timeout(5):
            await draining
        # then
        self.assertFalse(finished_early)
        self.assertTrue(await self.has("f"))

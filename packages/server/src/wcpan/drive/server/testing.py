"""Run a backend's code against a real core in tests, without a server.

``backend_harness`` provides a temporary mirror database with its write and
enrichment workers. Each ``add_instance`` gives one backend instance the same
``CoreServices`` and ``BackendContext`` the server would, with fixed mount IDs
and fake media probes.
"""

import asyncio
import shutil
import tempfile
from collections.abc import AsyncGenerator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from wcpan.drive.protocol.types import NodeId

from ._backend import (
    BackendContext,
    BackendLifecycle,
    BackendRoutes,
    CoreServices,
    enter_backend,
    load_command_parser,
)
from ._lib.write_queue import create_write_queue, write_worker
from ._services.ids import NodeIds
from ._services.media.probe.runner import ProbeRunner
from ._services.media.probe.source import (
    MediaProbeError,
    MediaSource,
    read_exactly,
)
from ._services.media.probing import MediaProbing
from ._services.media.retries import FIRST_DELAY, MediaRetries
from ._services.mirror.model import BackendNamespace, BackendSchema
from ._services.mirror.store import MirrorStore
from ._services.mounts import Mount, Mounts
from ._services.off_main import OffMainService
from ._services.sync.metadata import create_metadata_queue, metadata_worker
from ._services.sync.mounts import ensure_mount_nodes
from ._services.sync.service import NodeSyncService
from ._types import ProbeFailure


__all__ = (
    "ROOT_ID",
    "BackendHarness",
    "BackendInstance",
    "BackendLifecycle",
    "FakeProbes",
    "ProbeRunner",
    "WriteGate",
    "backend_harness",
    "enter_backend",
    "load_command_parser",
    "make_mounts",
    "mount_id",
    "read_exactly",
)

ROOT_ID = NodeId("root")
"""The fixed ID of the server root."""


def mount_id(name: str) -> NodeId:
    """The fixed ID of the mount *name*."""
    return NodeId(f"_{name}")


def make_mounts(sources: Mapping[str, str]) -> Mounts:
    """Mounts named by the keys of *sources*, under ``ROOT_ID``."""
    return Mounts(
        (
            Mount(name=name, id=mount_id(name), source=source)
            for name, source in sources.items()
        ),
        root_id=ROOT_ID,
    )


class FakeProbes:
    """Stands in for probe workers: reads the whole file through its source.

    Content starting with ``invalid_prefix`` is invalid media; anything else is
    4x3. ``transient`` probes fail as transient before any other outcome.
    """

    def __init__(self) -> None:
        self.invalid_prefix: bytes | None = b"bad"
        self.transient = 0
        self.probed: list[bytes] = []

    async def probe(self, source: MediaSource, *, is_image: bool) -> tuple[int, int]:
        data = await read_exactly(source, 0, source.size) if source.size else b""
        self.probed.append(data)
        if self.transient:
            self.transient -= 1
            raise MediaProbeError(ProbeFailure.TRANSIENT, "connection reset")
        if not await source.unchanged():
            raise MediaProbeError(ProbeFailure.TRANSIENT, "the file changed")
        if self.invalid_prefix is not None and data.startswith(self.invalid_prefix):
            raise MediaProbeError(ProbeFailure.INVALID, "unreadable")
        return 4, 3


class WriteGate:
    """Holds the write worker until ``release``, so writes queue up behind it."""

    def __init__(self) -> None:
        self._entered = asyncio.Event()
        self._released = asyncio.Event()

    async def hold(self) -> None:
        self._entered.set()
        await self._released.wait()

    async def entered(self) -> None:
        await self._entered.wait()

    def release(self) -> None:
        self._released.set()


@dataclass(frozen=True, kw_only=True)
class BackendInstance:
    """What the server would give one backend instance."""

    name: str
    core: CoreServices
    context: BackendContext
    mounts: Mounts
    namespace: BackendNamespace


class BackendHarness:
    """A running core over a temporary database."""

    def __init__(
        self,
        *,
        mirror: MirrorStore,
        database_path: Path,
        node_sync: NodeSyncService,
        media: MediaProbing,
        probes: FakeProbes,
        retries: MediaRetries,
        make_ids: Callable[[int], NodeIds],
    ) -> None:
        self.mirror = mirror
        self.database_path = database_path
        self.probes = probes
        self._node_sync = node_sync
        self._media = media
        self._retries = retries
        self._make_ids = make_ids

    async def add_instance(
        self, name: str, schema: BackendSchema, sources: Mapping[str, str]
    ) -> BackendInstance:
        """Register instance *name*, serving a mount per item of *sources*."""
        node_sync = self._node_sync.for_backend(name)
        namespace = await node_sync.register_schema(schema)
        mounts = make_mounts(sources)
        await ensure_mount_nodes(mounts, mirror=self.mirror, node_sync=self._node_sync)
        core = CoreServices(
            mirror=self.mirror,
            node_sync=node_sync,
            ids=self._make_ids(namespace.key),
            media=self._media,
        )
        context = BackendContext(
            name=name,
            mounts=mounts,
            core=core,
            routes=BackendRoutes(name=name),
            access=None,
        )
        return BackendInstance(
            name=name, core=core, context=context, mounts=mounts, namespace=namespace
        )

    async def hold_writes(self) -> WriteGate:
        """Stop the write worker; returns once it is held."""
        gate = WriteGate()
        await self._node_sync.enqueue(gate.hold)
        await gate.entered()
        return gate

    def pending_retries(self, name: str) -> set[str]:
        """Source keys of instance *name* with a media retry scheduled or running."""
        return self._retries.pending(name)


@asynccontextmanager
async def backend_harness(
    *, first_retry_delay: float = FIRST_DELAY
) -> AsyncGenerator[BackendHarness]:
    """A core over a new temporary database; everything stops on exit.

    A failed write or enrichment worker stops the server, so here it interrupts
    the block when the block is waiting, and is raised on exit.
    """
    directory = Path(tempfile.mkdtemp(prefix="wcpan_drive_test_"))
    pool = ThreadPoolExecutor(2)
    tasks: list[asyncio.Task[None]] = []
    owner = asyncio.current_task()
    interrupted = False

    def interrupt_on_failure(task: asyncio.Task[None]) -> None:
        nonlocal interrupted
        failed = not task.cancelled() and task.exception() is not None
        if failed and owner is not None and not owner.done() and not interrupted:
            interrupted = True
            owner.cancel()

    try:
        database_path = directory / "mirror.sqlite"
        mirror = MirrorStore(str(database_path), off_main=OffMainService(pool=pool))
        await mirror.ensure_schema()
        write_queue = create_write_queue()
        metadata_queue = create_metadata_queue()
        writer = asyncio.create_task(write_worker(write_queue))
        tasks.append(writer)
        probes = FakeProbes()
        media = MediaProbing(runner=cast(ProbeRunner, probes))
        retries = MediaRetries(first_delay=first_retry_delay)
        node_sync = NodeSyncService(
            mirror=mirror,
            write_queue=write_queue,
            metadata_queue=metadata_queue,
            probing=media,
            retries=retries,
            writer=writer,
        )
        tasks.append(
            asyncio.create_task(
                metadata_worker(metadata_queue, node_sync.process_metadata_item)
            )
        )

        def partial_ids(key: int) -> NodeIds:
            return NodeIds(mirror=mirror, write_queue=write_queue, key=key)

        for task in tasks:
            task.add_done_callback(interrupt_on_failure)
        yield BackendHarness(
            mirror=mirror,
            database_path=database_path,
            node_sync=node_sync,
            media=media,
            probes=probes,
            retries=retries,
            make_ids=partial_ids,
        )
    finally:
        failure = next(
            (
                error
                for task in tasks
                if task.done()
                and not task.cancelled()
                and (error := task.exception()) is not None
            ),
            None,
        )
        for task in tasks:
            task.remove_done_callback(interrupt_on_failure)
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(directory, ignore_errors=True)
        if interrupted and owner is not None and not owner.done():
            owner.uncancel()
        if failure is not None:
            raise failure

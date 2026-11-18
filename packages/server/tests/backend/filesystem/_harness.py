"""Shared fixture: a real mirror database over a temporary mount directory."""

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._backend import CoreServices
from wcpan.drive.server._lib.write_queue import (
    create_write_queue,
    write_worker,
)
from wcpan.drive.server._services.ids import NodeIds
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import (
    MediaProbeError,
    MediaSource,
    ProbeFailure,
    read_exactly,
)
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.mounts import Mount, Mounts, ensure_mount_nodes
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.sync.metadata import (
    create_metadata_queue,
    metadata_worker,
)
from wcpan.drive.server._services.sync.service import NodeSyncService
from wcpan.drive.server.backend.filesystem._blocking import Blocking
from wcpan.drive.server.backend.filesystem._media import media_sources
from wcpan.drive.server.backend.filesystem._operations import (
    FilesystemOperations,
    FilesystemUploader,
)
from wcpan.drive.server.backend.filesystem._paths import NodePaths
from wcpan.drive.server.backend.filesystem._scan import FilesystemScanner
from wcpan.drive.server.backend.filesystem._store import (
    FILESYSTEM_SCHEMA,
    FilesystemStore,
)
from wcpan.drive.server.backend.filesystem._watch import DirectoryWatcher


ROOT_ID = NodeId("root")
"""The fixed ID of the server root in ``make_mounts``."""


def mount_id(name: str) -> NodeId:
    """The fixed ID ``make_mounts`` gives the mount *name*."""
    return NodeId(f"_{name}")


def make_mounts(sources: Mapping[str, str]) -> Mounts:
    """Mounts named by the keys of *sources*, with IDs from ``mount_id``,
    under ``ROOT_ID``."""
    return Mounts(
        (
            Mount(name=name, id=mount_id(name), source=source)
            for name, source in sources.items()
        ),
        root_id=ROOT_ID,
    )


DOCS = mount_id("docs")

logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


class FakeProbes:
    """Stands in for probe workers: reads the whole file through its source.

    Content starting with ``invalid_prefix`` is invalid media; anything else
    is 4x3.
    """

    def __init__(self) -> None:
        self.invalid_prefix: bytes | None = b"bad"
        # Probes that fail as transient before any other outcome.
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


class ScanTestCase(IsolatedAsyncioTestCase):
    watch = False
    read_only = False
    sweep_staging_after: float | None = None

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wcpan_fs_"))
        self.source = self.tmp / "docs"
        self.source.mkdir()
        self.db_path = str(self.tmp / "mirror.sqlite")
        self.pool = ThreadPoolExecutor(2)
        self.io_pool = ThreadPoolExecutor(2)
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"fs": FILESYSTEM_SCHEMA},
        )
        await self.mirror.ensure_schema()
        self.write_queue = create_write_queue()
        self.metadata_queue = create_metadata_queue()
        self.probes = FakeProbes()
        self.media = MediaProbing(runner=cast(ProbeRunner, self.probes))
        # The core's service, which alone writes the root and mount nodes.
        self.core_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=self.write_queue,
            probing=self.media,
            metadata_queue=self.metadata_queue,
        )
        # The backend's own view, as the server hands it over.
        self.node_sync = self.core_sync.for_backend("fs")
        self.core = CoreServices(
            mirror=self.mirror,
            node_sync=self.node_sync,
            off_main=MagicMock(),
            ids=MagicMock(),
            media=self.media,
        )
        self.scans = self.core.scans
        self.ids = NodeIds(
            mirror=self.mirror,
            write_queue=self.write_queue,
            key=self.mirror.namespace("fs").key,
        )
        self.tasks = [
            asyncio.create_task(write_worker(self.write_queue)),
            asyncio.create_task(
                metadata_worker(
                    self.metadata_queue, self.node_sync.process_metadata_item
                )
            ),
        ]
        self.store = FilesystemStore(
            mirror=self.mirror,
            ns=self.mirror.namespace("fs"),
            write=self.core.enqueue_write,
        )
        self.mounts = make_mounts({"docs": str(self.source)})
        self.paths = NodePaths(mirror=self.mirror, mounts=self.mounts)
        blocking = Blocking(executor=self.io_pool)
        self.enterContext(self.media.register("fs", media_sources(blocking)))
        self.watcher = DirectoryWatcher.create() if self.watch else None
        if self.watcher is not None:
            self.watcher.start()
        self.scanner = FilesystemScanner(
            mirror=self.mirror,
            store=self.store,
            enrichment=self.core.enrichment,
            scans=self.scans,
            ids=self.ids,
            paths=self.paths,
            blocking=blocking,
            watcher=self.watcher,
            sweep_staging_after=self.sweep_staging_after,
        )
        self.operations = FilesystemOperations(
            scanner=self.scanner,
            mirror=self.mirror,
            scans=self.scans,
            paths=self.paths,
            store=self.store,
            blocking=blocking,
            read_only=self.read_only,
        )
        self.uploader = FilesystemUploader(
            scanner=self.scanner,
            mirror=self.mirror,
            scans=self.scans,
            blocking=blocking,
            media=self.media,
            read_only=self.read_only,
        )
        await ensure_mount_nodes(
            self.mounts, mirror=self.mirror, node_sync=self.core_sync
        )

    async def asyncTearDown(self) -> None:
        if self.watcher is not None:
            self.watcher.close()
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.io_pool.shutdown(wait=False, cancel_futures=True)
        for path in self.tmp.rglob("*"):
            with suppress(OSError):
                path.chmod(0o700)
        shutil.rmtree(self.tmp)

    async def scan(self) -> None:
        await self.scanner.scan()
        await self.node_sync.wait_enrichment_drained()

    def write(self, relative: str, data: bytes) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def node(self, relative: str) -> NodeRecord | None:
        """The mirrored node at *relative*, walking names from the mount."""
        parent = DOCS
        record: NodeRecord | None = None
        for name in relative.split("/"):
            children = await self.mirror.get_children(parent)
            record = next((c for c in children if c.name == name), None)
            if record is None:
                return None
            parent = record.id
        return record

    async def node_id(self, relative: str) -> NodeId:
        record = await self.node(relative)
        assert record is not None, relative
        return record.id

    async def cursor(self) -> int:
        return await self.mirror.get_cursor()

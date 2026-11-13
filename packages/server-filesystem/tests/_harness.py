"""Shared fixture: a real mirror database over a temporary mount directory."""

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.filesystem._blocking import Blocking
from wcpan.drive.server.filesystem._operations import (
    FilesystemOperations,
    FilesystemUploader,
)
from wcpan.drive.server.filesystem._paths import NodePaths
from wcpan.drive.server.filesystem._scan import FilesystemScanner
from wcpan.drive.server.filesystem._store import FILESYSTEM_SCHEMA, FilesystemStore
from wcpan.drive.server.filesystem._watch import DirectoryWatcher
from wcpan.drive.server.services.ids import NodeIds
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.mounts import Mount, Mounts, ensure_mount_nodes
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)


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
        # The core's service, which alone writes the root and mount nodes.
        self.core_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=self.write_queue,
            off_main=OffMainService(pool=self.pool),
            mounts={},
            metadata_queue=self.metadata_queue,
        )
        # The backend's own view, as the server hands it over.
        self.node_sync = self.core_sync.for_backend("fs")
        self.ids = NodeIds(
            mirror=self.mirror,
            write_queue=self.write_queue,
            key=self.mirror.namespace("fs").key,
        )
        self.tasks = [
            asyncio.create_task(write_worker(self.write_queue, noop_after_write)),
            asyncio.create_task(
                metadata_worker(
                    self.metadata_queue, self.node_sync.process_metadata_item
                )
            ),
        ]
        self.store = FilesystemStore(mirror=self.mirror, ns=self.mirror.namespace("fs"))
        self.mounts = make_mounts({"docs": str(self.source)})
        self.paths = NodePaths(mirror=self.mirror, mounts=self.mounts)
        blocking = Blocking(executor=self.io_pool)
        self.watcher = DirectoryWatcher.create() if self.watch else None
        if self.watcher is not None:
            self.watcher.start()
        self.scanner = FilesystemScanner(
            mirror=self.mirror,
            store=self.store,
            node_sync=self.node_sync,
            ids=self.ids,
            paths=self.paths,
            blocking=blocking,
            watcher=self.watcher,
            sweep_staging_after=self.sweep_staging_after,
        )
        self.operations = FilesystemOperations(
            scanner=self.scanner,
            mirror=self.mirror,
            node_sync=self.node_sync,
            paths=self.paths,
            store=self.store,
            blocking=blocking,
            read_only=self.read_only,
        )
        self.uploader = FilesystemUploader(
            scanner=self.scanner,
            mirror=self.mirror,
            node_sync=self.node_sync,
            blocking=blocking,
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

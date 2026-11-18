"""Shared fixture: a real mirror database over a temporary mount directory."""

import logging
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
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
from wcpan.drive.server.testing import ROOT_ID, backend_harness, mount_id


__all__ = ("DOCS", "ROOT_ID", "ScanTestCase")

DOCS = mount_id("docs")

logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


class ScanTestCase(IsolatedAsyncioTestCase):
    watch = False
    read_only = False
    sweep_staging_after: float | None = None
    first_retry_delay = 3600.0

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wcpan_fs_"))
        self.source = self.tmp / "docs"
        self.source.mkdir()
        self.io_pool = ThreadPoolExecutor(2)
        self.server = await self.enterAsyncContext(
            backend_harness(first_retry_delay=self.first_retry_delay)
        )
        self.mirror = self.server.mirror
        self.probes = self.server.probes
        instance = await self.server.add_instance(
            "fs", FILESYSTEM_SCHEMA, {"docs": str(self.source)}
        )
        self.core = instance.core
        self.scans = self.core.scans
        self.mounts = instance.mounts
        self.ids = self.core.ids
        self.media = self.core.media
        self.store = FilesystemStore(
            mirror=self.mirror, ns=instance.namespace, write=self.core.enqueue_write
        )
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

    async def asyncTearDown(self) -> None:
        if self.watcher is not None:
            self.watcher.close()
        self.io_pool.shutdown(wait=False, cancel_futures=True)
        for path in self.tmp.rglob("*"):
            with suppress(OSError):
                path.chmod(0o700)
        shutil.rmtree(self.tmp)

    async def scan(self) -> None:
        await self.scanner.scan()
        await self.core.enrichment.drain()

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

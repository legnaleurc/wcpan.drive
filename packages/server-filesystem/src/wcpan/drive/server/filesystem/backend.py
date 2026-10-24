"""The local filesystem as a ``Backend`` of the generic server."""

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging import getLogger

from aiohttp import web
from wcpan.drive.server.backend import CoreServices

from .blocking import Blocking
from .config import FilesystemConfig
from .operations import FilesystemOperations, FilesystemUploader
from .paths import NodePaths
from .scan import FilesystemScanner
from .store import FILESYSTEM_SCHEMA, FilesystemSchema, FilesystemStore
from .watch import DirectoryWatcher


_L = getLogger(__name__)

# Concurrent listings, hashes, and reads.
_IO_WORKERS = 4
# Seconds to wait after a change event before re-reading.
_SETTLE_DELAY = 1.0


class FilesystemRuntime:
    def __init__(
        self,
        *,
        operations: FilesystemOperations,
        uploader: FilesystemUploader,
        scanner: FilesystemScanner,
        watcher: DirectoryWatcher | None,
        rescan_interval: float,
        core: CoreServices,
        settle_delay: float = _SETTLE_DELAY,
    ) -> None:
        self._operations = operations
        self._uploader = uploader
        self._scanner = scanner
        self._watcher = watcher
        self._rescan_interval = rescan_interval
        self._settle_delay = settle_delay
        self._core = core
        self._scanned = asyncio.Event()

    @property
    def operations(self) -> FilesystemOperations:
        return self._operations

    @property
    def uploader(self) -> FilesystemUploader:
        return self._uploader

    async def run_until_ready(self) -> None:
        # Events during the startup scan are kept and handled once it finishes.
        if self._watcher is not None:
            self._watcher.start()
        await self._scanner.ensure_structure()
        await self._scanner.scan()
        await self._core.node_sync.retry_pending_enrichment(locate=self._scanner.locate)
        await self._core.node_sync.wait_enrichment_drained()
        self._scanned.set()
        _L.info("Startup scan applied")

    async def run(self) -> None:
        await self._scanned.wait()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._rescan_periodically())
            if self._watcher is not None:
                group.create_task(self._follow_changes(self._watcher))

    async def _rescan_periodically(self) -> None:
        """Full rescans catch anything the watcher missed."""
        while True:
            await asyncio.sleep(self._rescan_interval)
            await self._apply(self._scanner.scan(), "Rescan")

    async def _follow_changes(self, watcher: DirectoryWatcher) -> None:
        while True:
            await watcher.wait()
            # Let a burst of changes settle into one pass.
            await asyncio.sleep(self._settle_delay)
            dirty, overflow = watcher.take()
            if overflow:
                _L.warning("Change events were lost; rescanning everything")
                await self._apply(self._scanner.scan(), "Rescan")
            else:
                await self._apply(self._scanner.scan_folders(dirty), "Update")

    async def _apply(self, work: Coroutine[None, None, None], what: str) -> None:
        try:
            await work
            await self._core.node_sync.wait_enrichment_drained()
        except Exception:
            _L.exception("%s failed; later changes or rescans will retry", what)


class FilesystemBackend:
    def __init__(self, *, config: FilesystemConfig, mounts: dict[str, str]) -> None:
        self._config = config
        self._mounts = mounts

    @property
    def schema(self) -> FilesystemSchema:
        return FILESYSTEM_SCHEMA

    def add_routes(self, app: web.Application) -> None:
        pass

    @asynccontextmanager
    async def start(
        self, app: web.Application, core: CoreServices
    ) -> AsyncGenerator[FilesystemRuntime, None]:
        executor = ThreadPoolExecutor(
            max_workers=_IO_WORKERS, thread_name_prefix="wcpan-fs"
        )
        watcher = DirectoryWatcher.create() if self._config.watch else None
        try:
            blocking = Blocking(executor=executor)
            store = FilesystemStore(storage=core.storage)
            paths = NodePaths(storage=core.storage, mounts=self._mounts)
            scanner = FilesystemScanner(
                storage=core.storage,
                store=store,
                node_sync=core.node_sync,
                paths=paths,
                blocking=blocking,
                watcher=watcher,
            )
            read_only = self._config.read_only
            yield FilesystemRuntime(
                operations=FilesystemOperations(
                    scanner=scanner,
                    storage=core.storage,
                    node_sync=core.node_sync,
                    paths=paths,
                    store=store,
                    blocking=blocking,
                    read_only=read_only,
                ),
                uploader=FilesystemUploader(
                    scanner=scanner,
                    storage=core.storage,
                    node_sync=core.node_sync,
                    blocking=blocking,
                    read_only=read_only,
                ),
                scanner=scanner,
                watcher=watcher,
                rescan_interval=self._config.rescan_interval,
                core=core,
            )
        finally:
            if watcher is not None:
                watcher.close()
            executor.shutdown(wait=False, cancel_futures=True)

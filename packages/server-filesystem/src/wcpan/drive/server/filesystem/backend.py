"""The local filesystem as a ``Backend`` of the generic server."""

import asyncio
from collections.abc import AsyncGenerator
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


_L = getLogger(__name__)

# Concurrent listings, hashes, and reads.
_IO_WORKERS = 4


class FilesystemRuntime:
    def __init__(
        self,
        *,
        operations: FilesystemOperations,
        scanner: FilesystemScanner,
        rescan_interval: float,
        core: CoreServices,
    ) -> None:
        self._operations = operations
        self._uploader = FilesystemUploader()
        self._scanner = scanner
        self._rescan_interval = rescan_interval
        self._core = core

    @property
    def operations(self) -> FilesystemOperations:
        return self._operations

    @property
    def uploader(self) -> FilesystemUploader:
        return self._uploader

    async def run_until_ready(self) -> None:
        await self._scanner.ensure_structure()
        await self._scanner.scan()
        await self._core.node_sync.retry_pending_enrichment(locate=self._scanner.locate)
        await self._core.node_sync.wait_enrichment_drained()
        _L.info("Startup scan applied")

    async def run(self) -> None:
        """Rescan periodically; there is no watcher yet."""
        while True:
            await asyncio.sleep(self._rescan_interval)
            try:
                await self._scanner.scan()
                await self._core.node_sync.wait_enrichment_drained()
            except Exception:
                _L.exception("Rescan failed; retrying after the next interval")


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
        try:
            blocking = Blocking(executor=executor)
            store = FilesystemStore(storage=core.storage)
            paths = NodePaths(storage=core.storage, mounts=self._mounts)
            yield FilesystemRuntime(
                operations=FilesystemOperations(
                    paths=paths, store=store, blocking=blocking
                ),
                scanner=FilesystemScanner(
                    storage=core.storage,
                    store=store,
                    node_sync=core.node_sync,
                    paths=paths,
                    blocking=blocking,
                ),
                rescan_interval=self._config.rescan_interval,
                core=core,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

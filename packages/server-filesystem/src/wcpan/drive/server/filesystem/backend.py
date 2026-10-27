"""The local filesystem as a ``Backend`` of the generic server."""

import asyncio
from collections.abc import AsyncGenerator, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging import getLogger
from pathlib import Path
from typing import Annotated

from wcpan.drive.server.backend import BackendContext, CoreServices
from wcpan.drive.server.services.paths import map_by_prefix

from .blocking import Blocking
from .commands import command_parser, rebind_mount
from .config import FilesystemConfig, check_mounts
from .operations import FilesystemOperations, FilesystemUploader
from .paths import NodePaths
from .scan import FilesystemScanner
from .store import FILESYSTEM_SCHEMA, FilesystemStore
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


@asynccontextmanager
async def create_backend(
    context: BackendContext,
    *,
    rescan_interval: Annotated[float, "Seconds between full rescans."] = 3600.0,
    watch: Annotated[bool, "Follow changes with inotify."] = True,
    read_only: Annotated[bool, "Reject every change through the server."] = False,
    local_paths: Annotated[
        dict[str, str] | None,
        "Source path prefix -> host path for media probing\n"
        "(default: probe the source paths themselves).",
    ] = None,
) -> AsyncGenerator["FilesystemBackend", None]:
    """Backend factory for ``type: wcpan.drive.server.filesystem.create_backend``."""
    config = FilesystemConfig(
        rescan_interval=rescan_interval,
        watch=watch,
        read_only=read_only,
        local_paths=local_paths,
    )
    mounts = check_mounts(context.mounts)
    await context.core.mirror.register_schema(context.name, FILESYSTEM_SCHEMA)
    backend = FilesystemBackend(context=context, config=config, mounts=mounts)
    if context.offline:
        yield backend
        return
    async with backend.serving():
        yield backend


class FilesystemBackend:
    """Local directories, serving the mounts assigned to this instance."""

    def __init__(
        self,
        *,
        context: BackendContext,
        config: FilesystemConfig,
        mounts: dict[str, str],
    ) -> None:
        self._context = context
        self._config = config
        self._mounts = mounts
        self._local_path = (
            map_by_prefix(config.local_paths)
            if config.local_paths is not None
            else Path
        )
        self._runtime: FilesystemRuntime | None = None

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def mounts(self) -> dict[str, str]:
        return self._mounts

    def local_path(self, source_path: str) -> Path | None:
        return self._local_path(source_path)

    @property
    def operations(self) -> FilesystemOperations:
        return self._require_runtime().operations

    @property
    def uploader(self) -> FilesystemUploader:
        return self._require_runtime().uploader

    async def run_until_ready(self) -> None:
        await self._require_runtime().run_until_ready()

    async def run(self) -> None:
        await self._require_runtime().run()

    async def command(self, argv: Sequence[str]) -> None:
        args = command_parser(self.name).parse_args(list(argv))
        if args.command == "rebind-mount":
            fsid = await rebind_mount(
                self._context.core.mirror, self._mounts, args.mount
            )
            print(f"Mount {args.mount!r} now expects filesystem {fsid}.")

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[None, None]:
        core = self._context.core
        executor = ThreadPoolExecutor(
            max_workers=_IO_WORKERS, thread_name_prefix="wcpan-fs"
        )
        watcher = DirectoryWatcher.create() if self._config.watch else None
        try:
            blocking = Blocking(executor=executor)
            store = FilesystemStore(mirror=core.mirror)
            paths = NodePaths(mirror=core.mirror, mounts=self._mounts)
            scanner = FilesystemScanner(
                mirror=core.mirror,
                store=store,
                node_sync=core.node_sync,
                paths=paths,
                blocking=blocking,
                watcher=watcher,
            )
            read_only = self._config.read_only
            self._runtime = FilesystemRuntime(
                operations=FilesystemOperations(
                    scanner=scanner,
                    mirror=core.mirror,
                    node_sync=core.node_sync,
                    paths=paths,
                    store=store,
                    blocking=blocking,
                    read_only=read_only,
                ),
                uploader=FilesystemUploader(
                    scanner=scanner,
                    mirror=core.mirror,
                    node_sync=core.node_sync,
                    blocking=blocking,
                    read_only=read_only,
                ),
                scanner=scanner,
                watcher=watcher,
                rescan_interval=self._config.rescan_interval,
                core=core,
            )
            yield
        finally:
            self._runtime = None
            if watcher is not None:
                watcher.close()
            executor.shutdown(wait=False, cancel_futures=True)

    def _require_runtime(self) -> FilesystemRuntime:
        if self._runtime is None:
            raise RuntimeError(f"backend {self.name!r} is not serving")
        return self._runtime

"""The local filesystem as a ``Backend`` of the generic server."""

import asyncio
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from logging import getLogger
from typing import Annotated

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.lib import UPLOAD_SESSION_TTL, backend_commands, writing
from wcpan.drive.server.types import (
    BackendCommand,
    BackendContext,
    ChangeHandler,
    CoreServices,
    Mounts,
)

from ._blocking import Blocking
from ._commands import parse_command, rebind_mount
from ._config import FilesystemConfig, check_mounts
from ._media import media_sources
from ._operations import FilesystemOperations, FilesystemUploader
from ._paths import NodePaths
from ._scan import FilesystemScanner
from ._store import FILESYSTEM_SCHEMA, FilesystemStore
from ._watch import DirectoryWatcher


_L = getLogger(__name__)

# Concurrent listings, hashes, and reads.
_IO_WORKERS = 4
# Seconds to wait after a change event before re-reading.
_SETTLE_DELAY = 1.0
# Seconds after its last write that a full scan deletes an upload staging file
# no upload of this process holds. Twice the session TTL, since the core aborts
# a session left idle that long, and another server sharing the folder may
# still be using one written up to a TTL ago.
STAGING_SWEEP_AFTER = 2 * UPLOAD_SESSION_TTL


@dataclass(frozen=True, slots=True)
class FolderChanges:
    """What the watcher collected over one settle window."""

    dirty: set[NodeId]
    overflow: bool


class _FollowChanges(ChangeHandler[FolderChanges]):
    # A failed pass is repaired by later changes or the next rescan.
    continue_after_failure = True

    def __init__(self, scanner: FilesystemScanner) -> None:
        self._scanner = scanner

    async def handle(self, event: FolderChanges) -> None:
        if event.overflow:
            _L.warning("Change events were lost; rescanning everything")
            await self._scanner.scan()
        else:
            await self._scanner.scan_folders(event.dirty)


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

    @property
    def operations(self) -> FilesystemOperations:
        return self._operations

    @property
    def uploader(self) -> FilesystemUploader:
        return self._uploader

    async def startup_scan(self) -> None:
        # Events during the scan stay in the watcher until ``run`` publishes them.
        if self._watcher is not None:
            self._watcher.start()
        await self._scanner.scan()
        _L.info("Startup scan applied")

    def change_handler(self) -> ChangeHandler[FolderChanges] | None:
        return None if self._watcher is None else _FollowChanges(self._scanner)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._rescan_periodically())
            if self._watcher is not None:
                group.create_task(self._publish_changes(self._watcher))

    async def _rescan_periodically(self) -> None:
        """Full rescans catch anything the watcher missed."""
        while True:
            await asyncio.sleep(self._rescan_interval)
            try:
                await self._scanner.scan()
            except Exception:
                _L.exception("Rescan failed; later changes or rescans will retry")

    async def _publish_changes(self, watcher: DirectoryWatcher) -> None:
        """Publish one batch at a time, so changes during a pass merge into one."""
        while True:
            await watcher.wait()
            # Let a burst of changes settle into one pass.
            await asyncio.sleep(self._settle_delay)
            dirty, overflow = watcher.take()
            self._core.hints.publish(FolderChanges(dirty, overflow))
            await self._core.hints.join()


@backend_commands(parse_command)
@asynccontextmanager
async def create_backend(
    context: BackendContext,
    *,
    rescan_interval: Annotated[float, "Seconds between full rescans."] = 3600.0,
    watch: Annotated[bool, "Follow changes with inotify."] = True,
    read_only: Annotated[bool, "Reject every change through the server."] = False,
) -> AsyncGenerator["FilesystemBackend", None]:
    """Backend factory for ``type: wcpan.drive.server.backend.filesystem.create_backend``."""
    config = FilesystemConfig(
        rescan_interval=rescan_interval,
        watch=watch,
        read_only=read_only,
    )
    mounts = check_mounts(context.mounts)
    ns = await context.core.bind_schema(FILESYSTEM_SCHEMA)
    core = context.core
    # Read-only commands read the mirror only; the store writes.
    store = (
        FilesystemStore(mirror=core.mirror, ns=ns)
        if isinstance(core, CoreServices)
        else None
    )
    backend = FilesystemBackend(
        context=context, config=config, mounts=mounts, store=store
    )
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
        mounts: Mounts,
        store: FilesystemStore | None,
    ) -> None:
        self._context = context
        self._config = config
        self._mounts = mounts
        self._store = store
        self._runtime: FilesystemRuntime | None = None

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def mounts(self) -> Mounts:
        return self._mounts

    @property
    def operations(self) -> FilesystemOperations:
        return self._require_runtime().operations

    @property
    def uploader(self) -> FilesystemUploader:
        return self._require_runtime().uploader

    @property
    def files(self) -> FilesystemUploader:
        return self._require_runtime().uploader

    async def startup_scan(self) -> None:
        await self._require_runtime().startup_scan()

    def change_handler(self) -> ChangeHandler[FolderChanges] | None:
        return self._require_runtime().change_handler()

    async def run(self) -> None:
        await self._require_runtime().run()

    async def run_command(self, command: BackendCommand) -> None:
        args = command.args
        if args.command == "rebind-mount":
            fsid, changed = await rebind_mount(
                self._require_store(), self._mounts, args.mount
            )
            if changed:
                print(f"Mount {args.mount!r} now expects filesystem {fsid}.")
            else:
                print(f"Mount {args.mount!r} is already bound to filesystem {fsid}.")
        elif args.command == "pending":
            mirror = self._context.core.mirror
            for failure in await mirror.get_media_failures(self.name):
                print(f"{failure.source_key}\t{failure.kind}")

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[None, None]:
        core = writing(self._context.core)
        executor = ThreadPoolExecutor(
            max_workers=_IO_WORKERS, thread_name_prefix="wcpan-fs"
        )
        watcher = DirectoryWatcher.create() if self._config.watch else None
        try:
            blocking = Blocking(executor=executor)
            store = self._require_store()
            paths = NodePaths(mirror=core.mirror, mounts=self._mounts)
            scanner = FilesystemScanner(
                mirror=core.mirror,
                store=store,
                node_sync=core.node_sync,
                scans=core.scans,
                ids=core.ids,
                paths=paths,
                blocking=blocking,
                watcher=watcher,
                sweep_staging_after=(
                    None if self._config.read_only else STAGING_SWEEP_AFTER
                ),
            )
            read_only = self._config.read_only
            self._runtime = FilesystemRuntime(
                operations=FilesystemOperations(
                    scanner=scanner,
                    mirror=core.mirror,
                    scans=core.scans,
                    paths=paths,
                    store=store,
                    blocking=blocking,
                    read_only=read_only,
                ),
                uploader=FilesystemUploader(
                    scanner=scanner,
                    mirror=core.mirror,
                    scans=core.scans,
                    blocking=blocking,
                    media=core.media,
                    read_only=read_only,
                ),
                scanner=scanner,
                watcher=watcher,
                rescan_interval=self._config.rescan_interval,
                core=core,
            )
            with core.media.register(self.name, media_sources(blocking)):
                async with core.node_sync.retrying_with(scanner.rescan_parent):
                    yield
        finally:
            self._runtime = None
            if watcher is not None:
                watcher.close()
            executor.shutdown(wait=False, cancel_futures=True)

    def _require_store(self) -> FilesystemStore:
        if self._store is None:
            raise RuntimeError(f"backend {self.name!r} is read-only in this command")
        return self._store

    def _require_runtime(self) -> FilesystemRuntime:
        if self._runtime is None:
            raise RuntimeError(f"backend {self.name!r} is not serving")
        return self._runtime

"""Synology Drive as a backend of the generic server."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger
from pathlib import Path
from typing import Annotated

from wcpan.drive.server.exceptions import LocateUndecidedError
from wcpan.drive.server.lib import (
    SpooledFiles,
    SpoolingUploader,
    backend_commands,
    create_spool_area,
    writing,
)
from wcpan.drive.server.types import (
    BackendCommand,
    BackendContext,
    BackendNamespace,
    MediaFailure,
    MirrorReader,
    Mounts,
    NodeObservation,
    NodeOrdering,
    NodeSyncService,
    Uploader,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyPermanentLink,
)

from ._backfill import BackfillService
from ._commands import parse_command, print_stats
from ._config import SynologyConfig, create_synology_client
from ._handlers import webhook_handler
from ._media import media_sources
from ._mounts import MountRegistry, create_mount_registry
from ._operations import SynologyNodeOperations
from ._paths import SynologyPathService
from ._scan import StartupScanService
from ._store import SYNOLOGY_SCHEMA, SynologyStore, SynologyStoreReader
from ._upload import SynologyUploader
from ._webhook import WebhookService


_L = getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class _Connection:
    drive_api: SynologyClient
    store: SynologyStoreReader
    registry: MountRegistry
    syno_paths: SynologyPathService


@dataclass(frozen=True, kw_only=True)
class _Serving:
    operations: SynologyNodeOperations
    uploader: Uploader
    files: SpooledFiles
    startup_scan: StartupScanService
    webhook_service: WebhookService


@backend_commands(parse_command)
@asynccontextmanager
async def create_backend(
    context: BackendContext,
    *,
    url: Annotated[
        str, "Synology DSM / Drive API base URL (scheme + host + port if needed)."
    ],
    username: Annotated[str, "Synology Drive user."],
    password: Annotated[str, "Password of that user."],
    webhook_callback_url: Annotated[
        str,
        "Externally reachable base URL of this server; the webhook is\n"
        "registered at <webhook_callback_url>/api/v1/backends/<name>/webhook.",
    ],
    webhook_app_id: Annotated[
        str,
        "Stable app ID of this server's webhook registration: an integer as a\n"
        'string, such as "1". Webhooks under it are replaced at startup, so\n'
        "each server sharing the NAS needs its own.",
    ],
    otp_code: Annotated[
        str | None, "TOTP code, if the account uses 2FA (when required by the API)."
    ] = None,
    upload_tmp_dir: Annotated[
        str | None,
        "Directory for resumable upload spool files (default: system temp dir).",
    ] = None,
) -> AsyncGenerator["SynologyBackend", None]:
    """Backend factory for ``type: wcpan.drive.server.backend.synology.create_backend``.

    Serving connects here; offline, ``command`` connects once its arguments parse.
    """
    config = SynologyConfig(
        url=url,
        username=username,
        password=password,
        public_url=webhook_callback_url,
        webhook_app_id=webhook_app_id,
        otp_code=otp_code,
        upload_tmp_dir=upload_tmp_dir,
    )
    ns = await context.core.bind_schema(SYNOLOGY_SCHEMA)
    backend = SynologyBackend(context=context, config=config, ns=ns)
    if context.offline:
        yield backend
        return
    async with backend.serving():
        yield backend


class SynologyBackend:
    """One Synology Drive server, serving the mounts assigned to it."""

    def __init__(
        self, *, context: BackendContext, config: SynologyConfig, ns: BackendNamespace
    ) -> None:
        self._context = context
        self._config = config
        self._ns = ns
        self._scan_done = asyncio.Event()
        self._serving: _Serving | None = None

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def config(self) -> SynologyConfig:
        return self._config

    @property
    def mounts(self) -> Mounts:
        return self._context.mounts

    @property
    def operations(self) -> SynologyNodeOperations:
        return self._require_serving().operations

    @property
    def uploader(self) -> Uploader:
        return self._require_serving().uploader

    @property
    def files(self) -> SpooledFiles:
        # The Drive API takes a file in one request, so resumable uploads spool.
        return self._require_serving().files

    async def run_until_ready(self) -> None:
        """Startup scan, then the webhooks that arrived meanwhile."""
        await self._require_serving().startup_scan.run_until_ready(self._scan_done)
        await writing(self._context.core).hints.join()

    async def run(self) -> None:
        webhook_service = self._require_serving().webhook_service
        # Webhooks wait, so the startup scan sees a stable mirror.
        await self._scan_done.wait()
        await writing(self._context.core).hints.run(webhook_service)

    async def run_command(self, command: BackendCommand) -> None:
        args = command.args
        if args.command == "backfill":
            path = args.path.strip()
            if args.dry_run:
                # Reads only, so it runs on whichever core the command got.
                core = self._context.core
                async with self.connect() as c:
                    stats = await self._backfill(
                        c, core.mirror, core.node_sync, path, dry_run=True
                    )
            else:
                writer = writing(self._context.core)
                async with (
                    self.connect() as c,
                    writer.node_sync.enrichment_scope(),
                ):
                    stats = await self._backfill(
                        c, writer.mirror, writer.node_sync, path, dry_run=False
                    )
            print_stats(stats)
        elif args.command == "pending":
            failures = await self._context.core.mirror.get_media_failures(self.name)
            if failures:
                # Logs in only to find where the withheld files are now.
                async with self.connect() as c:
                    await self._print_media_failures(c, failures)

    async def _backfill(
        self,
        c: "_Connection",
        mirror: MirrorReader,
        node_sync: NodeSyncService | NodeOrdering,
        path: str,
        *,
        dry_run: bool,
    ) -> dict[str, int]:
        service = BackfillService(
            drive_api=c.drive_api,
            mirror=mirror,
            store=c.store,
            syno_paths=c.syno_paths,
            node_sync=node_sync,
        )
        return await service.run_virtual_path(path, dry_run=dry_run)

    async def _print_media_failures(
        self, c: "_Connection", failures: list[MediaFailure]
    ) -> None:
        mirror = self._context.core.mirror
        for failure in failures:
            link = failure.source_key
            try:
                observation = await c.syno_paths.locate_link(
                    c.drive_api, SynologyPermanentLink(permanent_link=link)
                )
            except LocateUndecidedError as e:
                print(f"{link}\t(unknown: {e})")
                continue
            source = (
                await self.mounts.source_path(observation.record, mirror=mirror)
                if observation is not None
                else None
            )
            if source is None:
                print(f"{link}\t(gone)")
                continue
            print(f"{source}\t{failure.kind}")

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[_Connection, None]:
        """Log in, resolve the mount roots, and probe media through this login."""
        async with create_synology_client(self._config) as drive_api:
            core = self._context.core
            store = SynologyStoreReader(mirror=core.mirror, ns=self._ns)
            registry = await create_mount_registry(
                self._context.mounts, drive_api=drive_api
            )
            with core.media.register(self.name, media_sources(drive_api)):
                yield _Connection(
                    drive_api=drive_api,
                    store=store,
                    registry=registry,
                    syno_paths=SynologyPathService(
                        registry=registry, store=store, ids=core.ids
                    ),
                )

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[None, None]:
        """Connect, receive webhooks, and build the services the server uses."""
        core = writing(self._context.core)
        store = SynologyStore(mirror=core.mirror, ns=self._ns)
        async with AsyncExitStack() as stack:
            c = await stack.enter_async_context(self.connect())
            path = self._context.routes.add(
                "POST", "webhook", webhook_handler(core.hints)
            )
            await stack.enter_async_context(
                _managed_webhook(
                    c.drive_api, self._config, url=f"{self._config.public_url}{path}"
                )
            )
            await stack.enter_async_context(
                core.node_sync.retrying(partial(_locate_link, c))
            )
            tmp_dir = self._config.upload_tmp_dir
            spool = stack.enter_context(
                create_spool_area(tmp_dir=Path(tmp_dir) if tmp_dir else None)
            )
            uploader = SynologyUploader(
                drive_api=c.drive_api,
                syno_paths=c.syno_paths,
                scans=core.scans,
            )
            # The Drive API takes a file in one request, so media is spooled
            # and probed before it is sent.
            files = SpooledFiles(uploader=uploader, area=spool, media=core.media)
            self._serving = _Serving(
                operations=SynologyNodeOperations(
                    drive_api=c.drive_api,
                    syno_paths=c.syno_paths,
                    scans=core.scans,
                ),
                uploader=SpoolingUploader(uploader=uploader, files=files),
                files=files,
                startup_scan=StartupScanService(
                    drive_api=c.drive_api,
                    mirror=core.mirror,
                    store=store,
                    syno_paths=c.syno_paths,
                    node_sync=core.node_sync,
                    scans=core.scans,
                ),
                webhook_service=WebhookService(
                    drive_api=c.drive_api,
                    mirror=core.mirror,
                    store=store,
                    scans=core.scans,
                    syno_paths=c.syno_paths,
                    mount_registry=c.registry,
                    hints=core.hints,
                ),
            )
            try:
                yield
            finally:
                self._serving = None

    def _require_serving(self) -> _Serving:
        if self._serving is None:
            raise RuntimeError(f"backend {self.name!r} is not serving")
        return self._serving


async def _locate_link(c: _Connection, link: str) -> NodeObservation | None:
    """The file with permanent link *link*, as the Drive reports it now."""
    return await c.syno_paths.locate_link(
        c.drive_api, SynologyPermanentLink(permanent_link=link)
    )


@asynccontextmanager
async def _managed_webhook(
    drive_api: SynologyClient, config: SynologyConfig, *, url: str
) -> AsyncGenerator[None, None]:
    stale = await drive_api.list_webhooks(app_id=config.webhook_app_id)
    for hook in stale:
        try:
            await drive_api.delete_webhook(
                webhook_id=str(hook["webhook_id"]), app_id=config.webhook_app_id
            )
        except Exception:
            _L.warning("Failed to remove stale webhook %s", hook.get("webhook_id"))
    webhook_id = await drive_api.create_webhook(url=url, app_id=config.webhook_app_id)
    _L.info("Webhook registered: id=%s", webhook_id)
    try:
        yield
    finally:
        try:
            await drive_api.delete_webhook(
                webhook_id=webhook_id, app_id=config.webhook_app_id
            )
            _L.info("Webhook unregistered: id=%s", webhook_id)
        except Exception:
            _L.warning("Failed to unregister webhook id=%s", webhook_id)

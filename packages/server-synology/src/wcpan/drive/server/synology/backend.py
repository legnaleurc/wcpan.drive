"""Synology Drive as a backend of the generic server."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Annotated

from wcpan.drive.server.backend import BackendContext
from wcpan.drive.server.services.paths import map_by_prefix
from wcpan.drive.server.services.spool import SpooledFiles, create_spool_area
from wcpan.synology import SynologyClient, SynologyPath

from .backfill import BackfillService
from .commands import command_parser, print_stats
from .config import DEFAULT_WEBHOOK_APP_ID, SynologyConfig, create_synology_client
from .handlers import webhook_handler
from .mounts import MountRegistry, create_mount_registry
from .operations import SynologyNodeOperations
from .paths import SynologyPathService
from .scan import StartupScanService
from .store import SYNOLOGY_SCHEMA, SynologyStore
from .upload import SynologyUploader
from .webhook import WebhookService, create_webhook_queue


_L = getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class _Connection:
    drive_api: SynologyClient
    store: SynologyStore
    registry: MountRegistry
    syno_paths: SynologyPathService


@dataclass(frozen=True, kw_only=True)
class _Serving:
    operations: SynologyNodeOperations
    uploader: SynologyUploader
    files: SpooledFiles
    startup_scan: StartupScanService
    webhook_service: WebhookService


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
        str, "Stable identifier for this app's webhook registration."
    ] = DEFAULT_WEBHOOK_APP_ID,
    otp_code: Annotated[
        str | None, "TOTP code, if the account uses 2FA (when required by the API)."
    ] = None,
    local_paths: Annotated[
        dict[str, str] | None,
        "Synology path prefix -> host path, for media metadata probing.",
    ] = None,
    upload_tmp_dir: Annotated[
        str | None,
        "Directory for resumable upload spool files (default: system temp dir).",
    ] = None,
) -> AsyncGenerator["SynologyBackend", None]:
    """Backend factory for ``type: wcpan.drive.server.synology.create_backend``.

    Serving connects here; offline, ``command`` connects once its arguments parse.
    """
    config = SynologyConfig(
        url=url,
        username=username,
        password=password,
        public_url=webhook_callback_url,
        webhook_app_id=webhook_app_id,
        otp_code=otp_code,
        local_paths=dict(local_paths or {}),
        upload_tmp_dir=upload_tmp_dir,
    )
    await context.core.mirror.register_schema(context.name, SYNOLOGY_SCHEMA)
    backend = SynologyBackend(context=context, config=config)
    if context.offline:
        yield backend
        return
    async with backend.serving():
        yield backend


class SynologyBackend:
    """One Synology Drive server, serving the mounts assigned to it."""

    def __init__(self, *, context: BackendContext, config: SynologyConfig) -> None:
        self._context = context
        self._config = config
        self._mounts = {
            name: SynologyPath(source) for name, source in context.mounts.items()
        }
        self._local_path = map_by_prefix(config.local_paths)
        self._webhook_queue = create_webhook_queue()
        self._scan_done = asyncio.Event()
        self._serving: _Serving | None = None

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def config(self) -> SynologyConfig:
        return self._config

    @property
    def mounts(self) -> dict[str, SynologyPath]:
        return self._mounts

    def local_path(self, source_path: str) -> Path | None:
        return self._local_path(source_path)

    @property
    def operations(self) -> SynologyNodeOperations:
        return self._require_serving().operations

    @property
    def uploader(self) -> SynologyUploader:
        return self._require_serving().uploader

    @property
    def files(self) -> SpooledFiles:
        # The Drive API takes a file in one request, so resumable uploads spool.
        return self._require_serving().files

    async def run_until_ready(self) -> None:
        """Startup scan, then the webhooks that arrived meanwhile."""
        await self._require_serving().startup_scan.run_until_ready(self._scan_done)
        await self._webhook_queue.join()

    async def run(self) -> None:
        webhook_service = self._require_serving().webhook_service
        async with asyncio.TaskGroup() as group:
            await webhook_service.run(self._webhook_queue, group, self._scan_done)

    async def command(self, argv: Sequence[str]) -> None:
        args = command_parser(self.name).parse_args(list(argv))
        if args.command == "backfill":
            async with self.connect() as c:
                service = BackfillService(
                    drive_api=c.drive_api,
                    mirror=self._context.core.mirror,
                    store=c.store,
                    syno_paths=c.syno_paths,
                    node_sync=self._context.core.node_sync,
                )
                stats = await service.run_virtual_path(
                    args.path.strip(), dry_run=args.dry_run
                )
            print_stats(stats)

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[_Connection, None]:
        """Log in and resolve the mount roots."""
        async with create_synology_client(self._config) as drive_api:
            store = SynologyStore(mirror=self._context.core.mirror)
            registry = await create_mount_registry(self._mounts, drive_api=drive_api)
            yield _Connection(
                drive_api=drive_api,
                store=store,
                registry=registry,
                syno_paths=SynologyPathService(registry=registry, store=store),
            )

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[None, None]:
        """Connect, receive webhooks, and build the services the server uses."""
        core = self._context.core
        async with AsyncExitStack() as stack:
            c = await stack.enter_async_context(self.connect())
            path = self._context.routes.add(
                "POST", "webhook", webhook_handler(self._webhook_queue)
            )
            await stack.enter_async_context(
                _managed_webhook(
                    c.drive_api, self._config, url=f"{self._config.public_url}{path}"
                )
            )
            tmp_dir = self._config.upload_tmp_dir
            spool = stack.enter_context(
                create_spool_area(tmp_dir=Path(tmp_dir) if tmp_dir else None)
            )
            uploader = SynologyUploader(
                drive_api=c.drive_api,
                syno_paths=c.syno_paths,
                node_sync=core.node_sync,
            )
            self._serving = _Serving(
                operations=SynologyNodeOperations(
                    drive_api=c.drive_api,
                    syno_paths=c.syno_paths,
                    node_sync=core.node_sync,
                ),
                uploader=uploader,
                files=SpooledFiles(uploader=uploader, area=spool),
                startup_scan=StartupScanService(
                    drive_api=c.drive_api,
                    mirror=core.mirror,
                    store=c.store,
                    syno_paths=c.syno_paths,
                    node_sync=core.node_sync,
                ),
                webhook_service=WebhookService(
                    drive_api=c.drive_api,
                    mirror=core.mirror,
                    store=c.store,
                    node_sync=core.node_sync,
                    syno_paths=c.syno_paths,
                    mount_registry=c.registry,
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

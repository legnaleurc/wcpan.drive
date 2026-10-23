"""Synology Drive as a ``Backend`` of the generic server."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from logging import getLogger

from aiohttp import web
from wcpan.drive.server.backend import CoreServices
from wcpan.synology import SynologyClient, SynologyPath

from .config import SynologyConfig, create_synology_client
from .handlers import handle_synology_webhook
from .keys import (
    MOUNT_REGISTRY_KEY,
    SYNOLOGY_DRIVE_API_KEY,
    SYNOLOGY_PATH_KEY,
    WEBHOOK_QUEUE_KEY,
)
from .mounts import create_mount_registry
from .operations import SynologyNodeOperations
from .paths import SynologyPathService
from .scan import StartupScanService
from .store import SYNOLOGY_SCHEMA, SynologySchema, SynologyStore
from .upload import SynologyUploader
from .webhook import WebhookQueue, WebhookService, create_webhook_queue


_L = getLogger(__name__)


class SynologyRuntime:
    def __init__(
        self,
        *,
        operations: SynologyNodeOperations,
        uploader: SynologyUploader,
        startup_scan: StartupScanService,
        webhook_service: WebhookService,
        webhook_queue: WebhookQueue,
    ) -> None:
        self._operations = operations
        self._uploader = uploader
        self._startup_scan = startup_scan
        self._webhook_service = webhook_service
        self._webhook_queue = webhook_queue
        self._scan_done = asyncio.Event()

    @property
    def operations(self) -> SynologyNodeOperations:
        return self._operations

    @property
    def uploader(self) -> SynologyUploader:
        return self._uploader

    async def run_until_ready(self) -> None:
        """Startup scan, then the webhooks that arrived meanwhile."""
        await self._startup_scan.run_until_ready(self._scan_done)
        await self._webhook_queue.join()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            await self._webhook_service.run(self._webhook_queue, group, self._scan_done)


class SynologyBackend:
    def __init__(
        self, *, config: SynologyConfig, mounts: dict[str, SynologyPath]
    ) -> None:
        self._config = config
        self._mounts = mounts

    @property
    def schema(self) -> SynologySchema:
        return SYNOLOGY_SCHEMA

    def add_routes(self, app: web.Application) -> None:
        app.router.add_post("/api/v1/synology-webhook", handle_synology_webhook)

    @asynccontextmanager
    async def start(
        self, app: web.Application, core: CoreServices
    ) -> AsyncGenerator[SynologyRuntime, None]:
        async with AsyncExitStack() as stack:
            drive_api = await stack.enter_async_context(
                create_synology_client(self._config)
            )
            store = SynologyStore(storage=core.storage)
            mount_registry = await create_mount_registry(
                self._mounts, drive_api=drive_api
            )
            syno_paths = SynologyPathService(registry=mount_registry, store=store)
            await stack.enter_async_context(_managed_webhook(drive_api, self._config))

            webhook_queue = create_webhook_queue()
            app[SYNOLOGY_DRIVE_API_KEY] = drive_api
            app[MOUNT_REGISTRY_KEY] = mount_registry
            app[SYNOLOGY_PATH_KEY] = syno_paths
            app[WEBHOOK_QUEUE_KEY] = webhook_queue

            yield SynologyRuntime(
                operations=SynologyNodeOperations(
                    drive_api=drive_api,
                    syno_paths=syno_paths,
                    node_sync=core.node_sync,
                ),
                uploader=SynologyUploader(
                    drive_api=drive_api,
                    syno_paths=syno_paths,
                    node_sync=core.node_sync,
                ),
                startup_scan=StartupScanService(
                    drive_api=drive_api,
                    storage=core.storage,
                    store=store,
                    syno_paths=syno_paths,
                    node_sync=core.node_sync,
                ),
                webhook_service=WebhookService(
                    drive_api=drive_api,
                    storage=core.storage,
                    store=store,
                    node_sync=core.node_sync,
                    syno_paths=syno_paths,
                    mount_registry=mount_registry,
                ),
                webhook_queue=webhook_queue,
            )


@asynccontextmanager
async def _managed_webhook(
    drive_api: SynologyClient, config: SynologyConfig
) -> AsyncGenerator[None, None]:
    stale = await drive_api.list_webhooks(app_id=config.webhook_app_id)
    for hook in stale:
        try:
            await drive_api.delete_webhook(
                webhook_id=str(hook["webhook_id"]), app_id=config.webhook_app_id
            )
        except Exception:
            _L.warning("Failed to remove stale webhook %s", hook.get("webhook_id"))
    webhook_id = await drive_api.create_webhook(
        url=f"{config.public_url}/api/v1/synology-webhook",
        app_id=config.webhook_app_id,
    )
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

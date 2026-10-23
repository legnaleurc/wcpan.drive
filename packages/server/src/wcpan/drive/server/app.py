"""aiohttp application factory, routes, and startup/shutdown lifecycle."""

import asyncio
from collections.abc import AsyncGenerator, Coroutine, Generator
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from logging import getLogger
from pathlib import Path

from aiohttp import web
from wcpan.drive.server.synology import create_synology_client
from wcpan.synology import SynologyClient

from .handlers.changes import get_changes, get_cursor, get_root
from .handlers.health import get_livez, get_readyz, put_null
from .handlers.nodes import (
    create_node,
    delete_node,
    download_node,
    get_node,
    update_node,
    upload_node,
)
from .handlers.upload import (
    create_upload_session,
    delete_upload_session,
    head_upload_session,
    patch_upload_chunk,
)
from .handlers.webhook import handle_synology_webhook
from .keys import (
    CHANGE_SERVICE_KEY,
    CONFIG_KEY,
    MOUNT_REGISTRY_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    STORAGE_KEY,
    SYNOLOGY_DRIVE_API_KEY,
    SYNOLOGY_PATH_KEY,
    UPLOAD_SERVICE_KEY,
    WEBHOOK_QUEUE_KEY,
    WRITE_QUEUE_KEY,
)
from .lib.mounts import MountRegistry, create_mount_registry
from .services.off_main import OffMainService
from .services.paths import SynologyPathService
from .services.scan import StartupScanService
from .services.storage import StorageService, create_storage_service
from .services.sync import NodeSyncService
from .services.synology_operations import SynologyNodeOperations
from .services.upload import create_upload_service
from .services.webhook import WebhookService
from .types import MetadataQueue, ServerConfig, WriteQueue
from .workers import (
    METADATA_WORKER_COUNT,
    create_checkpoint_scheduler,
    create_metadata_queue,
    create_webhook_queue,
    create_write_queue,
    metadata_worker,
    write_worker,
)


_L = getLogger(__name__)


@contextmanager
def _managed_pool() -> Generator[ProcessPoolExecutor, None, None]:
    pool = ProcessPoolExecutor()
    try:
        yield pool
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@asynccontextmanager
async def _background[T](
    group: asyncio.TaskGroup, c: Coroutine[None, None, T]
) -> AsyncGenerator[None, None]:
    task = group.create_task(c)
    try:
        yield
    finally:
        task.cancel()


@asynccontextmanager
async def _managed_webhook(
    drive_api: SynologyClient, config: ServerConfig
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


@asynccontextmanager
async def managed_off_main() -> AsyncGenerator[OffMainService, None]:
    with _managed_pool() as pool:
        off_main = OffMainService(pool=pool)
        yield off_main


@asynccontextmanager
async def _managed_background_tasks(
    app: web.Application,
    drive_api: SynologyClient,
    storage: StorageService,
    write_queue: WriteQueue,
    mount_registry: MountRegistry,
    node_sync: NodeSyncService,
    metadata_queue: MetadataQueue,
) -> AsyncGenerator[None, None]:
    scan_done_event = asyncio.Event()
    webhook_queue = create_webhook_queue()
    app[READY_KEY] = False
    app[WEBHOOK_QUEUE_KEY] = webhook_queue

    async def _flip_ready() -> None:
        await scan_done_event.wait()
        await webhook_queue.join()
        app[READY_KEY] = True

    startup_scan = StartupScanService(
        drive_api=drive_api,
        storage=storage,
        syno_paths=app[SYNOLOGY_PATH_KEY],
        node_sync=node_sync,
    )

    webhook_service = WebhookService(
        drive_api=drive_api,
        storage=storage,
        node_sync=node_sync,
        syno_paths=app[SYNOLOGY_PATH_KEY],
        write_queue=write_queue,
        mount_registry=mount_registry,
    )

    async with AsyncExitStack() as stack:
        group = await stack.enter_async_context(asyncio.TaskGroup())
        schedule_checkpoint = create_checkpoint_scheduler(group, storage)
        await stack.enter_async_context(
            _background(group, write_worker(write_queue, schedule_checkpoint))
        )
        for _ in range(METADATA_WORKER_COUNT):
            await stack.enter_async_context(
                _background(
                    group,
                    metadata_worker(
                        metadata_queue,
                        node_sync.process_metadata_item,
                    ),
                )
            )
        await stack.enter_async_context(
            _background(
                group,
                startup_scan.run_until_ready(scan_done_event),
            )
        )
        await stack.enter_async_context(_background(group, _flip_ready()))
        await stack.enter_async_context(
            _background(group, app[UPLOAD_SERVICE_KEY].cleanup_expired_sessions())
        )
        await stack.enter_async_context(
            _background(
                group,
                webhook_service.run(webhook_queue, group, scan_done_event),
            )
        )
        yield


async def _app_lifecycle(app: web.Application) -> AsyncGenerator[None, None]:
    config: ServerConfig = app[CONFIG_KEY]
    async with AsyncExitStack() as stack:
        drive_api: SynologyClient = await stack.enter_async_context(
            create_synology_client(config)
        )
        off_main = await stack.enter_async_context(managed_off_main())
        storage = await create_storage_service(config.database_url, off_main=off_main)
        write_queue = create_write_queue()
        app[STORAGE_KEY] = storage
        app[OFF_MAIN_KEY] = off_main
        app[WRITE_QUEUE_KEY] = write_queue
        app[SYNOLOGY_DRIVE_API_KEY] = drive_api

        mount_registry = await create_mount_registry(
            config.mounts,
            drive_api=drive_api,
        )
        app[MOUNT_REGISTRY_KEY] = mount_registry
        app[SYNOLOGY_PATH_KEY] = SynologyPathService(
            registry=mount_registry,
            storage=storage,
        )

        await stack.enter_async_context(_managed_webhook(drive_api, config))

        metadata_queue = create_metadata_queue()
        node_sync = NodeSyncService(
            storage=storage,
            write_queue=write_queue,
            off_main=off_main,
            mounts=config.mounts,
            local_paths=config.local_paths,
            metadata_queue=metadata_queue,
        )
        app[CHANGE_SERVICE_KEY] = node_sync
        app[NODE_OPERATIONS_KEY] = SynologyNodeOperations(
            drive_api=drive_api,
            syno_paths=app[SYNOLOGY_PATH_KEY],
            node_sync=node_sync,
        )
        tmp_dir = Path(config.upload_tmp_dir) if config.upload_tmp_dir else None
        app[UPLOAD_SERVICE_KEY] = stack.enter_context(
            create_upload_service(
                tmp_dir=tmp_dir,
                node_sync=node_sync,
                drive_api=drive_api,
                syno_paths=app[SYNOLOGY_PATH_KEY],
            )
        )

        await stack.enter_async_context(
            _managed_background_tasks(
                app,
                drive_api,
                storage,
                write_queue,
                mount_registry,
                node_sync,
                metadata_queue,
            )
        )

        yield


def create_app(config: ServerConfig) -> web.Application:
    app = web.Application(
        client_max_size=8 * 1024 * 1024
    )  # 8 MiB limit for JSON/form bodies; upload endpoints use iter_chunked and bypass this
    app[CONFIG_KEY] = config
    app.cleanup_ctx.append(_app_lifecycle)

    _add_routes(app)
    return app


def _add_routes(app: web.Application) -> None:
    app.router.add_get("/livez", get_livez)
    app.router.add_get("/readyz", get_readyz)
    app.router.add_put("/null", put_null)
    app.router.add_get("/api/v1/cursor", get_cursor)
    app.router.add_get("/api/v1/root", get_root)
    app.router.add_get("/api/v1/changes", get_changes)
    app.router.add_get("/api/v1/nodes/{id}", get_node)
    app.router.add_get("/api/v1/nodes/{id}/download", download_node)
    app.router.add_post("/api/v1/nodes", create_node)
    app.router.add_patch("/api/v1/nodes/{id}", update_node)
    app.router.add_delete("/api/v1/nodes/{id}", delete_node)
    app.router.add_post("/api/v1/nodes/{parent_id}", upload_node)
    app.router.add_post("/api/v1/nodes/{parent_id}/uploads", create_upload_session)
    app.router.add_patch("/api/v1/uploads/{session_id}", patch_upload_chunk)
    app.router.add_head("/api/v1/uploads/{session_id}", head_upload_session)
    app.router.add_delete("/api/v1/uploads/{session_id}", delete_upload_session)
    app.router.add_post("/api/v1/synology-webhook", handle_synology_webhook)

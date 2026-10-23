"""aiohttp application factory, routes, and startup/shutdown lifecycle."""

import asyncio
from collections.abc import AsyncGenerator, Coroutine, Generator
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from logging import getLogger
from pathlib import Path

from aiohttp import web
from aiohttp.typedefs import Handler

from .backend import Backend, BackendRuntime, CoreServices
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
from .keys import (
    CHANGE_SERVICE_KEY,
    CONFIG_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    STORAGE_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from .services.off_main import OffMainService
from .services.operations import OperationNotSupportedError
from .services.storage import StorageService, create_storage_service
from .services.sync import NodeSyncService
from .services.upload import create_upload_service
from .types import MetadataQueue, ServerConfig, WriteQueue
from .workers import (
    METADATA_WORKER_COUNT,
    create_checkpoint_scheduler,
    create_metadata_queue,
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
async def managed_off_main() -> AsyncGenerator[OffMainService, None]:
    with _managed_pool() as pool:
        off_main = OffMainService(pool=pool)
        yield off_main


@asynccontextmanager
async def _managed_background_tasks(
    app: web.Application,
    runtime: BackendRuntime,
    storage: StorageService,
    write_queue: WriteQueue,
    node_sync: NodeSyncService,
    metadata_queue: MetadataQueue,
) -> AsyncGenerator[None, None]:
    app[READY_KEY] = False

    async def _flip_ready() -> None:
        await runtime.run_until_ready()
        app[READY_KEY] = True
        _L.info("Server ready")

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
        await stack.enter_async_context(_background(group, _flip_ready()))
        await stack.enter_async_context(
            _background(group, app[UPLOAD_SERVICE_KEY].cleanup_expired_sessions())
        )
        await stack.enter_async_context(_background(group, runtime.run()))
        yield


def _create_lifecycle(backend: Backend):
    async def _app_lifecycle(app: web.Application) -> AsyncGenerator[None, None]:
        config: ServerConfig = app[CONFIG_KEY]
        async with AsyncExitStack() as stack:
            off_main = await stack.enter_async_context(managed_off_main())
            storage = await create_storage_service(
                config.database_url, off_main=off_main, backend=backend.schema
            )
            write_queue = create_write_queue()
            app[STORAGE_KEY] = storage
            app[OFF_MAIN_KEY] = off_main
            app[WRITE_QUEUE_KEY] = write_queue

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

            runtime = await stack.enter_async_context(
                backend.start(
                    app,
                    CoreServices(
                        storage=storage, node_sync=node_sync, off_main=off_main
                    ),
                )
            )
            app[NODE_OPERATIONS_KEY] = runtime.operations
            tmp_dir = Path(config.upload_tmp_dir) if config.upload_tmp_dir else None
            app[UPLOAD_SERVICE_KEY] = stack.enter_context(
                create_upload_service(tmp_dir=tmp_dir, uploader=runtime.uploader)
            )

            await stack.enter_async_context(
                _managed_background_tasks(
                    app,
                    runtime,
                    storage,
                    write_queue,
                    node_sync,
                    metadata_queue,
                )
            )

            yield

    return _app_lifecycle


@web.middleware
async def _unsupported_middleware(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    try:
        return await handler(request)
    except OperationNotSupportedError as e:
        raise web.HTTPForbidden(reason=str(e) or "Not supported by this backend")


def create_app(config: ServerConfig, *, backend: Backend) -> web.Application:
    app = web.Application(
        client_max_size=8 * 1024 * 1024,
        middlewares=[_unsupported_middleware],
    )  # 8 MiB limit for JSON/form bodies; upload endpoints use iter_chunked and bypass this
    app[CONFIG_KEY] = config
    app.cleanup_ctx.append(_create_lifecycle(backend))

    _add_routes(app)
    backend.add_routes(app)
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

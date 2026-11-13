"""aiohttp application factory, routes, and startup/shutdown lifecycle."""

import asyncio
from collections.abc import (
    AsyncGenerator,
    Callable,
    Coroutine,
    Generator,
    Iterable,
    Mapping,
)
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from aiohttp import web
from aiohttp.typedefs import Handler

from .backend import (
    BACKEND_ROUTE_PREFIX,
    Backend,
    BackendContext,
    BackendRoutes,
    CoreServices,
    LazyLocalPaths,
    dispatch_backend_route,
    enter_backend,
    load_factory,
)
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
    CONFIG_KEY,
    MIRROR_KEY,
    NODE_OPERATIONS_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    UPLOAD_SERVICE_KEY,
    WRITE_QUEUE_KEY,
)
from .lib.write_queue import run_queued
from .services.ids import NodeIds
from .services.mirror import MirrorStore, create_mirror_store
from .services.mounts import Mounts, ensure_mount_nodes, load_mounts
from .services.off_main import OffMainService
from .services.operations import OperationInvalidError, OperationNotSupportedError
from .services.paths import MountSource
from .services.routing import BackendRouter
from .services.sync import NodeSyncService
from .services.upload import UploadService, UploadSessionStore
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


@dataclass(frozen=True, kw_only=True)
class Core:
    mirror: MirrorStore
    node_sync: NodeSyncService
    write_queue: WriteQueue
    metadata_queue: MetadataQueue
    off_main: OffMainService
    local_paths: LazyLocalPaths
    mounts: Mounts


@asynccontextmanager
async def managed_core(
    config: ServerConfig,
    *,
    off_main: OffMainService,
    after_write: Callable[[], None] | None = None,
) -> AsyncGenerator[Core, None]:
    """The mirror store, the write and metadata workers, and ``NodeSyncService``.

    Backend schemas are registered later, by each factory. Also used by
    offline commands, which run with the server stopped.
    """
    mirror = await create_mirror_store(config.database_url, off_main=off_main)
    for name in await mirror.registered_instances():
        if name not in config.backends:
            # Kept until dropped, so a config typo does not discard a mirror.
            _L.warning(
                "Backend %r is registered but not configured; its nodes stay"
                " in the mirror and cannot be read or changed",
                name,
            )
    write_queue = create_write_queue()
    metadata_queue = create_metadata_queue()
    local_paths = LazyLocalPaths({n: m.backend for n, m in config.mounts.items()})
    mounts = await load_mounts(
        {n: m.source for n, m in config.mounts.items()}, mirror=mirror
    )
    node_sync = NodeSyncService(
        mirror=mirror,
        write_queue=write_queue,
        off_main=off_main,
        mounts={
            m.id: MountSource(source=m.source, local_path=local_paths.for_mount(m.name))
            for m in mounts.values()
        },
        metadata_queue=metadata_queue,
    )
    async with AsyncExitStack() as stack:
        group = await stack.enter_async_context(asyncio.TaskGroup())
        if after_write is None:
            checkpoints = create_checkpoint_scheduler(group, mirror, write_queue)
            stack.callback(checkpoints.cancel)
            after_write = checkpoints
        await stack.enter_async_context(
            _background(group, write_worker(write_queue, after_write))
        )
        for _ in range(METADATA_WORKER_COUNT):
            await stack.enter_async_context(
                _background(
                    group,
                    metadata_worker(metadata_queue, node_sync.process_metadata_item),
                )
            )
        yield Core(
            mirror=mirror,
            node_sync=node_sync,
            write_queue=write_queue,
            metadata_queue=metadata_queue,
            off_main=off_main,
            local_paths=local_paths,
            mounts=mounts,
        )


@asynccontextmanager
async def entered_backends(
    config: ServerConfig,
    core: Core,
    *,
    offline: bool,
    names: Iterable[str] | None = None,
    routes: Mapping[str, BackendRoutes] | None = None,
) -> AsyncGenerator[dict[str, Backend], None]:
    """Enter the factories of *names* (default: every configured instance)."""
    selected = list(config.backends) if names is None else list(names)
    async with AsyncExitStack() as stack:
        backends: dict[str, Backend] = {}
        for name in selected:
            settings = config.backends[name]
            key = await run_queued(
                core.write_queue, partial(core.mirror.register_instance, name)
            )
            context = BackendContext(
                name=name,
                mounts=core.mounts.subset(config.mounts_of(name)),
                core=CoreServices(
                    mirror=core.mirror,
                    node_sync=core.node_sync.for_backend(name),
                    off_main=core.off_main,
                    ids=NodeIds(
                        mirror=core.mirror, write_queue=core.write_queue, key=key
                    ),
                ),
                routes=routes[name] if routes is not None else BackendRoutes(name=name),
                offline=offline,
            )
            factory = load_factory(settings.type)
            try:
                backend = await stack.enter_async_context(
                    enter_backend(factory, context, settings.settings)
                )
            except ValueError as e:
                raise ValueError(f"backends.{name}: {e}") from e
            core.local_paths.bind(name, backend)
            backends[name] = backend
        yield backends


def _create_lifecycle(routes: Mapping[str, BackendRoutes]):
    async def _app_lifecycle(app: web.Application) -> AsyncGenerator[None, None]:
        config: ServerConfig = app[CONFIG_KEY]
        app[READY_KEY] = False
        async with AsyncExitStack() as stack:
            off_main = await stack.enter_async_context(managed_off_main())
            core = await stack.enter_async_context(
                managed_core(config, off_main=off_main)
            )
            app[MIRROR_KEY] = core.mirror
            app[OFF_MAIN_KEY] = off_main
            app[WRITE_QUEUE_KEY] = core.write_queue

            await ensure_mount_nodes(
                core.mounts, mirror=core.mirror, node_sync=core.node_sync
            )
            released = await run_queued(
                core.write_queue,
                partial(
                    core.mirror.release_reassigned_mounts,
                    {m.id: config.mounts[n].backend for n, m in core.mounts.items()},
                ),
            )
            if released:
                _L.info("Removed %d subtree(s) left under reassigned mounts", released)
            backends = await stack.enter_async_context(
                entered_backends(config, core, offline=False, routes=routes)
            )
            router = BackendRouter(
                mirror=core.mirror,
                mounts=core.mounts,
                mount_backends={n: m.backend for n, m in config.mounts.items()},
                operations={n: b.operations for n, b in backends.items()},
                uploaders={n: b.uploader for n, b in backends.items()},
                files={n: b.files for n, b in backends.items()},
            )
            app[NODE_OPERATIONS_KEY] = router
            upload_service = UploadService(
                store=UploadSessionStore(), uploader=router, files=router
            )
            # Unwinds before the backends close.
            stack.push_async_callback(upload_service.close)
            app[UPLOAD_SERVICE_KEY] = upload_service

            async def _flip_ready() -> None:
                async with asyncio.TaskGroup() as group:
                    for backend in backends.values():
                        group.create_task(backend.run_until_ready())
                app[READY_KEY] = True
                _L.info("Server ready")

            group = await stack.enter_async_context(asyncio.TaskGroup())
            await stack.enter_async_context(_background(group, _flip_ready()))
            await stack.enter_async_context(
                _background(group, upload_service.cleanup_expired_sessions())
            )
            for backend in backends.values():
                await stack.enter_async_context(_background(group, backend.run()))

            yield

    return _app_lifecycle


@web.middleware
async def _operation_errors_middleware(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    try:
        return await handler(request)
    except OperationNotSupportedError as e:
        raise web.HTTPForbidden(reason=str(e) or "Not supported by this backend")
    except OperationInvalidError as e:
        raise web.HTTPBadRequest(reason=str(e))


def create_app(config: ServerConfig) -> web.Application:
    """The server for *config*; backend factories run when the app starts."""
    app = web.Application(
        client_max_size=8 * 1024 * 1024,
        middlewares=[_operation_errors_middleware],
    )  # 8 MiB limit for JSON/form bodies; upload endpoints use iter_chunked and bypass this
    app[CONFIG_KEY] = config
    routes = {name: BackendRoutes(name=name) for name in config.backends}
    app.cleanup_ctx.append(_create_lifecycle(routes))

    _add_routes(app)

    async def backend_route(request: web.Request) -> web.StreamResponse:
        return await dispatch_backend_route(request, routes)

    app.router.add_route(
        "*", f"{BACKEND_ROUTE_PREFIX}/{{backend}}/{{path:.*}}", backend_route
    )
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

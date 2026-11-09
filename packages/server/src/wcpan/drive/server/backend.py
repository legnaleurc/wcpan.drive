"""The contract between the generic server and storage backends.

A backend is an async context manager factory, named in the config by its import
path (``type: wcpan.drive.server.synology.create_backend``) like the client's
service factories. Its keyword-only parameters are its settings, as for client
plugins (see ``wcpan.drive.plugin``); packages register factories under the
``wcpan.drive.server.backend`` entry point group for ``init``. The core checks
the configured settings against them and calls ``factory(context, **settings)``
for each configured instance; the factory registers its schema and routes
through ``context``, connects to its source, and yields a ``Backend``. Several
instances, of the same or different backends, can serve different mounts of one
server.

In offline mode (maintenance commands, server stopped) a factory must not
connect or start background work; its ``command`` connects only if the parsed
command needs it, so ``--help`` and bad arguments never reach the source.
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from aiohttp import web
from aiohttp.typedefs import Handler
from wcpan.drive.plugin import check_settings

from .services.mirror import MirrorStore
from .services.off_main import OffMainService
from .services.operations import NodeOperations
from .services.sync import NodeSyncService
from .services.upload import Uploader, UploadStaging


BACKEND_ROUTE_PREFIX = "/api/v1/backends"
BACKEND_PLUGIN_GROUP = "wcpan.drive.server.backend"


def backend_route(backend: str, path: str) -> str:
    """The public path of route *path* registered by instance *backend*."""
    return f"{BACKEND_ROUTE_PREFIX}/{backend}/{path}"


@dataclass(frozen=True, kw_only=True)
class CoreServices:
    """Server services a backend instance builds on.

    ``node_sync`` tags every observation with the instance name.
    """

    mirror: MirrorStore
    node_sync: NodeSyncService
    off_main: OffMainService


@dataclass(kw_only=True)
class BackendRoutes:
    """Routes of one instance, served under ``/api/v1/backends/<name>/``.

    The application router is frozen before factories run, so the core owns one
    route for every backend and dispatches to handlers registered here.
    """

    name: str
    _handlers: dict[tuple[str, str], Handler] = field(
        default_factory=dict[tuple[str, str], Handler]
    )

    def add(self, method: str, path: str, handler: Handler) -> str:
        """Register *handler*; returns the public path to advertise."""
        self._handlers[(method.upper(), path)] = handler
        return backend_route(self.name, path)

    def find(self, method: str, path: str) -> Handler | None:
        return self._handlers.get((method.upper(), path))


@dataclass(frozen=True, kw_only=True)
class BackendContext:
    """What the core hands a backend factory, besides the instance's settings."""

    name: str
    # Mount name to source, for the mounts this instance serves.
    mounts: Mapping[str, str]
    core: CoreServices
    routes: BackendRoutes
    # Maintenance mode: no connection or background work until a command runs.
    offline: bool


class Backend(Protocol):
    """One entered backend instance.

    ``operations``, ``uploader``, ``staging``, ``run_until_ready``, and ``run``
    are used only when serving; ``command`` only offline.
    """

    def local_path(self, source_path: str) -> Path | None:
        """Host path for media probing of a file at *source_path*, if readable."""
        ...

    @property
    def operations(self) -> NodeOperations: ...

    @property
    def uploader(self) -> Uploader: ...

    @property
    def staging(self) -> UploadStaging | None:
        """Native staging for resumable uploads.

        ``None`` spools them locally and sends each through ``uploader``.
        """
        ...

    async def run_until_ready(self) -> None:
        """Initial discovery; return once its mounts are current enough to serve."""
        ...

    async def run(self) -> None:
        """Ongoing background work such as notifications; runs until cancelled."""
        ...

    async def command(self, argv: Sequence[str]) -> None:
        """Run a maintenance command; ``argv`` follows ``backend <name>``.

        Parses its own arguments (``SystemExit`` for ``--help`` or errors), and
        connects to the source only when the command needs it.
        """
        ...


type BackendFactory = Callable[..., AbstractAsyncContextManager[Backend]]
"""Called as ``factory(context, **settings)``; raises ``ValueError`` for bad settings.

Settings are keyword-only parameters, annotated with ``Annotated[T, "help"]``.
"""


def load_factory(path: str) -> BackendFactory:
    """Import the backend factory at dotted *path* (``module.function``)."""
    module_name, _, attribute = path.rpartition(".")
    if not module_name:
        raise ValueError(f"Backend type {path!r} is not a module.function path")
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as e:
        raise ValueError(
            f"Cannot load backend {path!r}; is its package installed?"
        ) from e
    factory: Any = getattr(module, attribute, None)
    if not callable(factory):
        raise ValueError(f"{module_name!r} has no backend factory {attribute!r}")
    return cast(BackendFactory, factory)


def enter_backend(
    factory: BackendFactory, context: BackendContext, settings: Mapping[str, Any]
) -> AbstractAsyncContextManager[Backend]:
    """Enter *factory* with *settings* checked against its parameters.

    Raises:
        ValueError: For an unknown, missing, or mistyped setting.
    """
    return factory(context, **check_settings(factory, settings))


class LazyLocalPaths:
    """Per-mount host path lookup, answered by backends once they are entered.

    ``NodeSyncService`` is built before any factory runs.
    """

    def __init__(self, mount_backends: Mapping[str, str]) -> None:
        self._mount_backends = mount_backends
        self._backends: dict[str, Backend] = {}

    def bind(self, name: str, backend: Backend) -> None:
        self._backends[name] = backend

    def for_mount(self, mount: str) -> Callable[[str], Path | None]:
        def local_path(source_path: str) -> Path | None:
            backend = self._backends.get(self._mount_backends[mount])
            return backend.local_path(source_path) if backend is not None else None

        return local_path


async def dispatch_backend_route(
    request: web.Request, routes: Mapping[str, BackendRoutes]
) -> web.StreamResponse:
    instance = routes.get(request.match_info["backend"])
    handler = (
        instance.find(request.method, request.match_info["path"])
        if instance is not None
        else None
    )
    if handler is None:
        raise web.HTTPNotFound()
    return await handler(request)

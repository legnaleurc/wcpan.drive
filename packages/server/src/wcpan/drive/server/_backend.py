"""The contract between the generic server and storage backends.

A backend is an async context manager factory, named in the config by its import
path (``type: wcpan.drive.server.backend.synology.create_backend``) like the client's
service factories. Its keyword-only parameters are its settings, as for client
plugins (see ``wcpan.drive.plugin``); packages register factories under the
``wcpan.drive.server.backend`` entry point group for ``init``. The core checks
the configured settings against them and calls ``factory(context, **settings)``
for each configured instance; the factory registers its schema and routes
through ``context``, connects to its source, and yields a ``Backend``. Several
instances, of the same or different backends, can serve different mounts of one
server.

Maintenance commands (server stopped) are parsed before anything starts: a
factory carries its parser (``backend_commands``), which turns the arguments
into a ``BackendCommand`` declaring what the command needs, so ``--help`` and
bad arguments exit before the database or the source is touched. The core then
enters the factory offline, where it must not connect or start background
work, and calls ``run_command``, which connects only if the command needs it.
"""

import argparse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from typing import Any, Protocol, cast

from aiohttp import web
from aiohttp.typedefs import Handler
from wcpan.drive.plugin import check_settings

from ._services.ids import NodeIdMap
from ._services.media.probing import MediaProbing
from ._services.mirror.model import (
    BackendNamespace,
    BackendSchema,
)
from ._services.mirror.store import (
    MirrorReader,
    MirrorStore,
)
from ._services.mounts import Mount as Mount
from ._services.mounts import Mounts
from ._services.off_main import OffMainService
from ._services.operations import NodeOperations
from ._services.sync.ordering import NodeOrdering
from ._services.sync.service import NodeSyncService
from ._services.upload.service import FileOpener, Uploader


BACKEND_ROUTE_PREFIX = "/api/v1/backends"
BACKEND_PLUGIN_GROUP = "wcpan.drive.server.backend"


def backend_route(backend: str, path: str) -> str:
    """The public path of route *path* registered by instance *backend*."""
    return f"{BACKEND_ROUTE_PREFIX}/{backend}/{path}"


@dataclass(frozen=True, kw_only=True)
class CoreServices:
    """Server services a backend instance builds on.

    ``node_sync`` tags every observation with the instance name. ``ids`` maps
    the instance's native IDs to the public node IDs it uses with the core.
    ``media`` probes the instance's files through the source it registers while
    connected, and probes upload spools.
    """

    mirror: MirrorStore
    node_sync: NodeSyncService
    off_main: OffMainService
    ids: NodeIdMap
    media: MediaProbing

    async def bind_schema(self, schema: BackendSchema) -> BackendNamespace:
        """Register the instance with *schema* (tables, orphan cleanup)."""
        return await self.node_sync.register_schema(schema)


@dataclass(frozen=True, kw_only=True)
class ReadCoreServices:
    """What a read-only maintenance command gets instead of ``CoreServices``.

    Field names match ``CoreServices``, so code that does not narrow
    ``BackendContext.core`` can use only what both offer: reads, read
    ordering, ID lookups that persist nothing, and media sources that cannot
    probe.
    """

    mirror: MirrorReader
    node_sync: NodeOrdering
    off_main: OffMainService
    ids: NodeIdMap
    media: MediaProbing
    schemas: Callable[[BackendSchema], Awaitable[BackendNamespace]]

    async def bind_schema(self, schema: BackendSchema) -> BackendNamespace:
        """Check the instance's tables against *schema*, changing nothing."""
        return await self.schemas(schema)


def writing(core: CoreServices | ReadCoreServices) -> CoreServices:
    """*core*, for work that writes; fails for a read-only command.

    Raises:
        RuntimeError: If *core* is read-only.
    """
    if not isinstance(core, CoreServices):
        raise RuntimeError("this needs write access, but the command is read-only")
    return core


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
    # The mounts this instance serves. Their node IDs are opaque.
    mounts: Mounts
    core: CoreServices | ReadCoreServices
    routes: BackendRoutes
    # A maintenance command's database access; ``None`` while serving. Offline,
    # a factory connects and starts nothing until the command runs.
    access: "Access | None"

    @property
    def offline(self) -> bool:
        return self.access is not None


class Access(Enum):
    """What a maintenance command does to the database."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, kw_only=True)
class BackendCommand:
    """A parsed maintenance command and what it needs from the core.

    ``probes`` is true for a command that probes media or queues enrichment.
    A command that reads must not probe, since probing records its results.
    """

    access: Access
    probes: bool = False
    args: argparse.Namespace

    def __post_init__(self) -> None:
        if self.access is Access.READ and self.probes:
            raise ValueError("a read-only command cannot probe media")


type CommandParser = Callable[[str, Sequence[str]], BackendCommand]
"""Called as ``parse(name, argv)`` with the arguments after ``backend NAME``.

Exits through ``SystemExit`` for ``--help`` and argument errors, like
``argparse``.
"""

_COMMAND_PARSER = "__wcpan_drive_command_parser__"


def backend_commands[F: Callable[..., Any]](parse: CommandParser) -> Callable[[F], F]:
    """Attach the maintenance command parser of the factory it decorates.

    Applied outermost, over ``@asynccontextmanager``; the factory's signature,
    which ``check_settings`` reads, is unchanged.
    """

    def attach(factory: F) -> F:
        setattr(factory, _COMMAND_PARSER, parse)
        return factory

    return attach


def load_command_parser(factory: Callable[..., Any]) -> CommandParser:
    """The command parser attached to *factory*; one that accepts none if absent."""
    parse = getattr(factory, _COMMAND_PARSER, None)
    return cast(CommandParser, parse) if parse is not None else _no_commands


def _no_commands(name: str, argv: Sequence[str]) -> BackendCommand:
    parser = argparse.ArgumentParser(
        prog=f"wcpan.drive.server backend {name}",
        description=f"Backend {name!r} has no maintenance commands.",
    )
    parser.parse_args(list(argv))
    parser.error("no command given")


class Backend(Protocol):
    """One entered backend instance.

    ``operations``, ``uploader``, ``files``, ``run_until_ready``, and ``run``
    are used only when serving; ``run_command`` only offline.
    """

    @property
    def operations(self) -> NodeOperations: ...

    @property
    def uploader(self) -> Uploader: ...

    @property
    def files(self) -> FileOpener:
        """Opens the random-access files that resumable uploads write.

        A backend that takes a file only whole can use ``SpooledFiles`` from
        ``wcpan.drive.server.lib``.
        """
        ...

    async def run_until_ready(self) -> None:
        """Initial discovery; return once its mounts are current enough to serve."""
        ...

    async def run(self) -> None:
        """Ongoing background work such as notifications; runs until cancelled."""
        ...

    async def run_command(self, command: BackendCommand) -> None:
        """Run a maintenance command its factory's parser produced.

        Connects to the source only when the command needs it.
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

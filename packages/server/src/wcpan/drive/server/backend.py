"""The contract between the generic server and a storage backend."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from .services.off_main import OffMainService
from .services.operations import NodeOperations
from .services.storage import BackendSchema, StorageService
from .services.sync import NodeSyncService
from .services.upload import Uploader


@dataclass(frozen=True, kw_only=True)
class CoreServices:
    """Server services a backend builds on."""

    storage: StorageService
    node_sync: NodeSyncService
    off_main: OffMainService


class BackendRuntime(Protocol):
    """A started backend."""

    @property
    def operations(self) -> NodeOperations: ...

    @property
    def uploader(self) -> Uploader: ...

    async def run_until_ready(self) -> None:
        """Initial discovery; return once the mirror is current enough to serve."""
        ...

    async def run(self) -> None:
        """Ongoing background work such as notifications; runs until cancelled."""
        ...


class Backend(Protocol):
    @property
    def schema(self) -> BackendSchema:
        """Persistence hooks for the backend's own tables."""
        ...

    def add_routes(self, app: web.Application) -> None:
        """Register backend-specific routes, such as notification endpoints."""
        ...

    def start(
        self, app: web.Application, core: CoreServices
    ) -> AbstractAsyncContextManager[BackendRuntime]:
        """Connect to the source; the runtime is valid until the context exits."""
        ...

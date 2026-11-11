"""A backend factory for tests: ``type: tests._fake_backend.create_backend``.

Entered instances are kept in ``instances`` by name so tests can drive them.
"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import MagicMock

from aiohttp import web
from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend import BackendContext
from wcpan.drive.server.services.mirror import BackendNamespace
from wcpan.drive.server.types import BackendLocator


class FakeSchema:
    name = "fake"
    version = 1

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        con.execute(
            "CREATE TABLE IF NOT EXISTS fake_backend (backend TEXT NOT NULL, x INTEGER)"
        )

    def bind_locator(
        self,
        con: sqlite3.Connection,
        ns: BackendNamespace,
        node_id: NodeId,
        locator: BackendLocator,
    ) -> None:
        pass

    def forget_nodes(
        self, con: sqlite3.Connection, ns: BackendNamespace, node_ids: Sequence[str]
    ) -> None:
        pass

    def forget_orphans(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        pass


class FakeBackend:
    def __init__(self, context: BackendContext, settings: dict[str, Any]) -> None:
        self.context = context
        self.settings = settings
        self.operations = MagicMock()
        self.uploader = MagicMock()
        self.files = MagicMock()
        self.discovered = asyncio.Event()
        self.running = asyncio.Event()
        self.commands: list[list[str]] = []

    def local_path(self, source_path: str) -> Path | None:
        return None

    async def run_until_ready(self) -> None:
        await self.discovered.wait()

    async def run(self) -> None:
        self.running.set()
        await asyncio.Event().wait()

    async def command(self, argv: Sequence[str]) -> None:
        self.commands.append(list(argv))


instances: dict[str, FakeBackend] = {}


@asynccontextmanager
async def create_backend(
    context: BackendContext, **settings: Any
) -> AsyncGenerator[FakeBackend, None]:
    if settings.get("invalid"):
        raise ValueError("invalid setting")
    await context.core.mirror.register_schema(context.name, FakeSchema())

    async def hook(_request: web.Request) -> web.Response:
        return web.Response(text=context.name)

    context.routes.add("POST", "hook", hook)
    backend = FakeBackend(context, settings)
    instances[context.name] = backend
    try:
        yield backend
    finally:
        instances.pop(context.name, None)


@asynccontextmanager
async def create_typed_backend(
    context: BackendContext,
    *,
    root: Annotated[str, "Root directory."],
    depth: int = 1,
) -> AsyncGenerator[FakeBackend, None]:
    async with create_backend(context, root=root, depth=depth) as backend:
        yield backend

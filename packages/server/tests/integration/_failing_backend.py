"""A backend whose state write fails, for ``test_write_failure``."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated
from unittest.mock import MagicMock

from wcpan.drive.server.lib import writing
from wcpan.drive.server.types import BackendCommand, BackendContext, CoreServices

from .._fake_backend import FakeSchema


def _invalid_sql(con: sqlite3.Connection) -> None:
    con.execute("NOT SQL")


class FailingBackend:
    def __init__(self, core: CoreServices, phase: str) -> None:
        self._core = core
        self._phase = phase
        self.operations = MagicMock()
        self.uploader = MagicMock()
        self.files = MagicMock()

    async def startup_scan(self) -> None:
        if self._phase == "startup":
            await self._core.enqueue_write(_invalid_sql)
            await self._core.writes_landed()

    def change_handler(self) -> None:
        return None

    async def run(self) -> None:
        if self._phase == "serving":
            await self._core.enqueue_write(_invalid_sql)
        elif self._phase == "draining":
            await self._core.enqueue_write(_invalid_sql)
            await self._core.enrichment.drain()
        await asyncio.Event().wait()

    async def run_command(self, command: BackendCommand) -> None:
        raise NotImplementedError


@asynccontextmanager
async def create_backend(
    context: BackendContext, *, phase: Annotated[str, "When the write fails."]
) -> AsyncGenerator[FailingBackend]:
    await context.core.bind_schema(FakeSchema())
    yield FailingBackend(writing(context.core), phase)

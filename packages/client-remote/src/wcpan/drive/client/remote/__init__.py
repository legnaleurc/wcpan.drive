from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from aiohttp import ClientSession
from wcpan.drive.client.types import Backend

from .service import RemoteBackend


@asynccontextmanager
async def create_backend(
    *, server_url: Annotated[str, "Base URL of the wcpan.drive server."]
) -> AsyncGenerator[Backend]:
    """Create a Backend that talks to a wcpan.drive.client.remote server."""
    async with ClientSession() as session:
        yield RemoteBackend(session=session, server_url=server_url)

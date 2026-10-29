from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from aiohttp import ClientSession
from wcpan.drive.client.types import Backend

from .service import FeedBackend


@asynccontextmanager
async def create_backend(
    *, server_url: Annotated[str, "Base URL of the wcpan.drive server."]
) -> AsyncGenerator[Backend]:
    """Create a Backend that talks to a wcpan.drive.server feed server."""
    async with ClientSession() as session:
        yield FeedBackend(session=session, server_url=server_url)

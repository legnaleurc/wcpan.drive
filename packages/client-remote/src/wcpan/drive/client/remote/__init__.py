from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from aiohttp import ClientSession
from wcpan.drive.client.types import FileService

from .service import ClientFileService


@asynccontextmanager
async def create_service(*, server_url: str) -> AsyncGenerator[FileService]:
    """Create a FileService that talks to a wcpan.drive.client.remote server."""
    async with ClientSession() as session:
        yield ClientFileService(session=session, server_url=server_url)

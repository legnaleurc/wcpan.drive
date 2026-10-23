"""Internal helper functions for lib module.

This module contains private implementation helpers used by the public
lib module. These functions are not part of the public API.
"""

import asyncio
from logging import getLogger
from pathlib import PurePath
from typing import BinaryIO

from .types import ReadableFile, WritableFile


_CHUNK_SIZE = 256 * 1024


def resolve_path(
    from_: PurePath,
    to: PurePath,
) -> PurePath:
    rv = from_
    for part in to.parts:
        if part == ".":
            continue
        elif part == "..":
            rv = rv.parent
        else:
            rv = rv / part
    return rv


async def upload_retry(
    fin: BinaryIO, fout: WritableFile, timeout: float | None
) -> None:
    while True:
        try:
            await _upload_feed(fin, fout, timeout)
            break
        except TimeoutError:
            getLogger(__name__).error("upload timeout, retry")

        await _upload_continue(fin, fout)


async def _upload_feed(
    fin: BinaryIO, fout: WritableFile, timeout: float | None
) -> None:
    while True:
        # Just in case the FS is unstable.
        async with asyncio.timeout(timeout):
            chunk = fin.read(_CHUNK_SIZE)
        if not chunk:
            break
        async with asyncio.timeout(timeout):
            await fout.write(chunk)
    async with asyncio.timeout(timeout):
        await fout.flush()


async def _upload_continue(fin: BinaryIO, fout: WritableFile) -> None:
    offset = await fout.tell()
    await fout.seek(offset)
    import os

    fin.seek(offset, os.SEEK_SET)


async def download_retry(
    fin: ReadableFile, fout: BinaryIO, timeout: float | None
) -> None:
    while True:
        try:
            await _download_feed(fin, fout, timeout)
            break
        except TimeoutError:
            getLogger(__name__).error("download timeout, retry")

        await _download_continue(fin, fout)


async def _download_feed(
    fin: ReadableFile, fout: BinaryIO, timeout: float | None
) -> None:
    while True:
        async with asyncio.timeout(timeout):
            chunk = await fin.read(_CHUNK_SIZE)
        if not chunk:
            break
        # Just in case the FS is unstable.
        async with asyncio.timeout(timeout):
            fout.write(chunk)
    # Just in case the FS is unstable.
    async with asyncio.timeout(timeout):
        fout.flush()


async def _download_continue(fin: ReadableFile, fout: BinaryIO) -> None:
    fout.flush()
    offset = fout.tell()
    await fin.seek(offset)

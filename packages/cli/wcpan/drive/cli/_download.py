from collections.abc import Iterable
from concurrent.futures import Executor
from pathlib import Path
from typing import override

from wcpan.drive.core.lib import download_file_to_local
from wcpan.drive.core.types import Drive, Node

from ._queue import AbstractHandler, walk_list
from .lib import get_file_hash


class DownloadHandler(AbstractHandler[Node, Path]):
    def __init__(self, *, drive: Drive, pool: Executor, include_trash: bool) -> None:
        self._drive = drive
        self._pool = pool
        self._include_trash = include_trash

    @override
    async def count_all(self, src: Node) -> int:
        total = 1
        async for _r, d, f in self._drive.walk(src):
            total += len(d) + len(f)
        return total

    @override
    def source_is_trashed(self, src: Node) -> bool:
        if self._include_trash:
            return False
        return src.is_trashed

    @override
    def source_is_directory(self, src: Node) -> bool:
        return src.is_directory

    @override
    async def do_directory(self, src: Node, dst: Path) -> Path:
        full_path = dst / src.name
        full_path.mkdir(parents=True, exist_ok=True)
        return full_path

    @override
    async def get_children(self, src: Node) -> list[Node]:
        return await self._drive.get_children(src)

    @override
    async def do_file(self, src: Node, dst: Path) -> None:
        local_src = dst / src.name
        if local_src.is_file():
            return

        if local_src.exists():
            raise RuntimeError(f"{local_src} is not a file")

        local_src = await download_file_to_local(self._drive, src, dst)
        local_hash = await get_file_hash(local_src, pool=self._pool, drive=self._drive)
        if local_hash != src.hash:
            raise RuntimeError(f"{dst} checksum mismatch")

    @override
    def format_source(self, src: Node) -> str:
        return src.name


async def download_list(
    srcs: Iterable[Node],
    dst: Path,
    *,
    drive: Drive,
    pool: Executor,
    jobs: int,
    fail_fast: bool,
    include_trash: bool,
) -> bool:
    handler = DownloadHandler(drive=drive, pool=pool, include_trash=include_trash)
    return await walk_list(handler, srcs, dst, jobs=jobs, fail_fast=fail_fast)

from collections.abc import Awaitable, Iterable
from concurrent.futures import Executor
from pathlib import Path
from typing import override

from wcpan.drive.core.lib import upload_file_from_local
from wcpan.drive.core.types import Drive, Node

from ._queue import AbstractHandler, walk_list
from .lib import get_file_hash, get_media_info


class UploadHandler(AbstractHandler[Path, Node]):
    def __init__(self, *, drive: Drive, pool: Executor) -> None:
        self._drive = drive
        self._pool = pool

    @override
    async def count_all(self, src: Path) -> int:
        total = 1
        for _root, folders, files in src.walk():
            total = total + len(folders) + len(files)
        return total

    @override
    def source_is_trashed(self, src: Path) -> bool:
        return False

    @override
    def source_is_directory(self, src: Path) -> bool:
        return src.is_dir()

    @override
    async def do_directory(self, src: Path, dst: Node) -> Node:
        folder_name = src.name
        node = await self._drive.create_directory(folder_name, dst, exist_ok=True)
        return node

    @override
    async def get_children(self, src: Path) -> list[Path]:
        return [_ for _ in src.iterdir()]

    @override
    async def do_file(self, src: Path, dst: Node) -> None:
        from mimetypes import guess_type

        node = await _else_none(self._drive.get_child_by_name(src.name, dst))
        if not node:
            type_, _ext = guess_type(src)
            media_info = get_media_info(src)
            node = await upload_file_from_local(
                self._drive, src, dst, mime_type=type_, media_info=media_info
            )
            if not node:
                raise Exception("upload failed")

        local_size = src.stat().st_size
        if local_size != node.size:
            raise Exception(f"{src} size mismatch")
        local_hash = await get_file_hash(src, pool=self._pool, drive=self._drive)
        if local_hash != node.hash:
            raise Exception(f"{src} checksum mismatch")

    @override
    def format_source(self, src: Path) -> str:
        return src.name


async def upload_list(
    srcs: Iterable[Path],
    dst: Node,
    *,
    drive: Drive,
    pool: Executor,
    jobs: int,
    fail_fast: bool,
) -> bool:
    handler = UploadHandler(drive=drive, pool=pool)
    return await walk_list(handler, srcs, dst, jobs=jobs, fail_fast=fail_fast)


async def _else_none(aw: Awaitable[Node]) -> Node | None:
    try:
        return await aw
    except Exception:
        return None

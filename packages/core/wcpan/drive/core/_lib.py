import asyncio
import os
from collections.abc import Awaitable, Callable
from logging import getLogger
from pathlib import Path, PurePath
from typing import BinaryIO, TypeGuard

from .exceptions import NodeExistsError, NodeIsADirectoryError, NodeNotFoundError
from .types import (
    ChangeAction,
    Drive,
    MediaInfo,
    Node,
    ReadableFile,
    RemoveAction,
    UpdateAction,
    WritableFile,
)


_DEFAULT_FILE_MIME_TYPE = "application/octet-stream"
_CHUNK_SIZE = 256 * 1024


def _resolve_path(
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


def normalize_path(path: PurePath) -> PurePath:
    if not path.is_absolute():
        raise ValueError("only accepts absolute path")
    rv: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        elif part == ".." and rv[-1] != "/":
            rv.pop()
        else:
            rv.append(part)
    return PurePath(*rv)


def is_valid_name(name: str) -> bool:
    if name.find("\\") >= 0:
        return False
    path = Path(name)
    return path.name == name


async def else_none[T](aw: Awaitable[T]) -> T | None:
    try:
        return await aw
    except NodeNotFoundError:
        return None


async def move_node(
    drive: Drive,
    src_path: PurePath,
    dst_path: PurePath,
) -> Node:
    src_node = await drive.get_node_by_path(src_path)

    # case 1 - move to a relative path
    if not dst_path.is_absolute():
        # case 1.1 - a name, not path
        if dst_path.name == dst_path:
            # case 1.1.1 - move to the same directory, do nothing
            if dst_path.name == ".":
                return src_node
            # case 1.1.2 - rename only
            if dst_path.name != "..":
                return await drive.move(
                    src_node,
                    new_parent=None,
                    new_name=dst_path.name,
                )
            # case 1.1.3 - move to parent directory, the same as case 1.2

        # case 1.2 - a relative path, resolve to absolute path
        # NOTE PurePath does not implement normalizing algorithm
        dst_path = _resolve_path(src_path.parent, dst_path)

    # case 2 - move to an absolute path
    dst_node = await else_none(drive.get_node_by_path(dst_path))
    # case 2.1 - the destination is empty
    if not dst_node:
        # move to the parent directory of the destination
        try:
            new_parent = await drive.get_node_by_path(dst_path.parent)
        except NodeNotFoundError as e:
            raise ValueError(f"no direct path to {dst_path}") from e
        return await drive.move(src_node, new_parent=new_parent, new_name=dst_path.name)
    # case 2.2 - the destination is a file
    if not dst_node.is_directory:
        # do not overwrite existing file
        raise NodeExistsError(dst_node)
    # case 2.3 - the distination is a directory
    return await drive.move(src_node, new_parent=dst_node, new_name=None)


async def find_duplicate_nodes(
    drive: Drive,
    root_node: Node | None = None,
) -> list[list[Node]]:
    if not root_node:
        root_node = await drive.get_root()

    rv: list[list[Node]] = []
    async for _root, directorys, files in drive.walk(root_node):
        nodes = directorys + files
        seen: dict[str, list[Node]] = {}
        for node in nodes:
            if node.name not in seen:
                seen[node.name] = [node]
            else:
                seen[node.name].append(node)
        for nodes in seen.values():
            if len(nodes) > 1:
                rv.append(nodes)

    return rv


async def upload_file_from_local(
    drive: Drive,
    path: Path,
    parent: Node,
    *,
    name: str | None = None,
    mime_type: str | None = None,
    media_info: MediaInfo | None = None,
    timeout: float | None = None,
) -> Node:
    # sanity check
    path = path.resolve()
    if not path.is_file():
        raise ValueError("invalid file")

    file_name = path.name if name is None else name
    total_file_size = path.stat().st_size
    if not mime_type:
        mime_type = _DEFAULT_FILE_MIME_TYPE

    async with drive.upload_file(
        name=file_name,
        parent=parent,
        size=total_file_size,
        mime_type=mime_type,
        media_info=media_info,
    ) as fout:
        with open(path, "rb") as fin:
            await _upload_retry(fin, fout, timeout)
        node = await fout.node()
    return node


async def _upload_retry(
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
    fin.seek(offset, os.SEEK_SET)


async def download_file_to_local(
    drive: Drive,
    node: Node,
    path: Path,
    *,
    timeout: float | None = None,
) -> Path:
    if node.is_directory:
        raise ValueError("cannot download a directory")

    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    # check if exists
    complete_path = path.joinpath(node.name)
    if complete_path.is_file():
        return complete_path

    # exists but not a file
    if complete_path.exists():
        raise NodeIsADirectoryError(complete_path)

    # if the file is empty, no need to download
    if node.size <= 0:
        open(complete_path, "w").close()
        return complete_path

    # resume download
    tmp_path = complete_path.parent.joinpath(f"{complete_path.name}.__tmp__")
    if tmp_path.is_file():
        offset = tmp_path.stat().st_size
        if offset > node.size:
            raise RuntimeError(
                f"local file size of `{complete_path}` is greater then remote"
                f" ({offset} > {node.size})"
            )
    elif tmp_path.exists():
        raise NodeIsADirectoryError(complete_path)
    else:
        offset = 0

    if offset < node.size:
        async with drive.download_file(node) as fin:
            await fin.seek(offset)
            with open(tmp_path, "ab") as fout:
                await _download_retry(fin, fout, timeout)

    # rename it back if completed
    tmp_path.rename(complete_path)

    return complete_path


async def _download_retry(
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


def is_remove(change: ChangeAction, /) -> TypeGuard[RemoveAction]:
    return change[0]


def is_update(change: ChangeAction, /) -> TypeGuard[UpdateAction]:
    return not change[0]


def dispatch_change[R](
    change: ChangeAction,
    /,
    *,
    on_remove: Callable[[str], R],
    on_update: Callable[[Node], R],
) -> R:
    match change:
        case (True, id_):
            return on_remove(id_)
        case (False, node):
            return on_update(node)

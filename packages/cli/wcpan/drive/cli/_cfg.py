from contextlib import asynccontextmanager
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from yaml import safe_load

from wcpan.drive.core import create_drive


class FunctionDict(TypedDict):
    name: str
    args: NotRequired[list[Any]]
    kwargs: NotRequired[dict[str, Any]]


class ServiceDict(TypedDict):
    main: FunctionDict
    middleware: NotRequired[list[FunctionDict]]


class DriveDict(TypedDict):
    file: ServiceDict
    snapshot: ServiceDict


class MainDict(TypedDict):
    version: int
    drive: DriveDict


@asynccontextmanager
async def create_drive_from_config(path: Path):
    with path.open("r") as fin:
        main: MainDict = safe_load(fin)

    version = main["version"]
    if version != 2:
        raise RuntimeError("wrong version")

    drive = main["drive"]
    file_ = drive["file"]
    file_main = _deserialize(file_["main"])
    file_middleware = [_deserialize(_) for _ in file_.get("middleware", [])]
    snapshot = drive["snapshot"]
    snapshot_main = _deserialize(snapshot["main"])
    snapshot_middleware = [_deserialize(_) for _ in snapshot.get("middleware", [])]

    async with create_drive(
        file=file_main,
        file_middleware=file_middleware,
        snapshot=snapshot_main,
        snapshot_middleware=snapshot_middleware,
    ) as drive:
        yield drive


def _deserialize(fragment: FunctionDict):
    name = fragment["name"]
    args = fragment.get("args", [])
    kwargs = fragment.get("kwargs", {})

    base, name = name.rsplit(".", 1)
    module = import_module(base)
    function = getattr(module, name)

    bound = partial(function, *args, **kwargs)
    return bound

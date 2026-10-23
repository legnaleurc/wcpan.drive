from contextlib import asynccontextmanager
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from wcpan.drive.client.api import compose_service, create_multi_drive
from wcpan.drive.client.types import SourceConfig
from yaml import safe_load


class FunctionDict(TypedDict):
    name: str
    args: NotRequired[list[Any]]
    kwargs: NotRequired[dict[str, Any]]


class SourceDict(TypedDict):
    name: str
    file: list[FunctionDict]
    snapshot: list[FunctionDict]


class MainDict(TypedDict):
    version: int
    sources: list[SourceDict]


@asynccontextmanager
async def create_drive_from_config(path: Path):
    with path.open("r") as fin:
        main: MainDict = safe_load(fin)

    version = main["version"]
    if version != 3:
        raise RuntimeError("wrong version")

    sources = [_parse_source(s) for s in main["sources"]]

    async with create_multi_drive(sources=sources) as drive:
        yield drive


def _parse_source(source: SourceDict) -> SourceConfig:
    file_fns = [_deserialize(f) for f in source["file"]]
    snap_fns = [_deserialize(f) for f in source["snapshot"]]
    return SourceConfig(
        name=source["name"],
        file=compose_service(file_fns[0], *file_fns[1:]),
        snapshot=compose_service(snap_fns[0], *snap_fns[1:]),
    )


def _deserialize(fragment: FunctionDict):
    name = fragment["name"]
    args = fragment.get("args", [])
    kwargs = fragment.get("kwargs", {})

    base, name = name.rsplit(".", 1)
    module = import_module(base)
    function = getattr(module, name)

    bound = partial(function, *args, **kwargs)
    return bound

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any

from wcpan.drive.client.api import compose_service, create_multi_drive
from wcpan.drive.client.types import Drive, SourceConfig
from yaml import safe_load


VERSION = 4


@dataclass(frozen=True, kw_only=True)
class _Function:
    name: str
    args: list[Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class _Source:
    name: str
    backend: list[_Function]
    cache: str


@asynccontextmanager
async def create_drive_from_config(path: Path) -> AsyncGenerator[Drive]:
    """Create a Drive from a YAML configuration file.

    The whole file is validated before any factory module is imported.
    Factory names are imported and called as written, so the file is trusted
    executable wiring; only load configuration you control.

    Raises:
        ValueError: If the configuration is malformed or has the wrong version.
    """
    with path.open("r") as fin:
        data: object = safe_load(fin)

    sources = [_resolve_source(_) for _ in _parse_main(data)]

    async with create_multi_drive(sources=sources) as drive:
        yield drive


def _parse_main(data: object) -> list[_Source]:
    main = _require_dict(data, "config")
    version = main.get("version")
    if version != VERSION:
        raise ValueError(f"unsupported config version: {version!r}")
    sources = _require_list(main.get("sources"), "sources")
    if not sources:
        raise ValueError("sources must not be empty")
    rv = [_parse_source(_, f"sources[{i}]") for i, _ in enumerate(sources)]
    names = [_.name for _ in rv]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate source names: {names}")
    return rv


def _parse_source(data: object, where: str) -> _Source:
    source = _require_dict(data, where)
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{where}.name must be a non-empty string")
    return _Source(
        name=name,
        backend=_parse_chain(source.get("backend"), f"{where}.backend"),
        cache=_parse_path(source.get("cache"), f"{where}.cache"),
    )


def _parse_chain(data: object, where: str) -> list[_Function]:
    chain = _require_list(data, where)
    if not chain:
        raise ValueError(f"{where} must not be empty")
    return [_parse_function(_, f"{where}[{i}]") for i, _ in enumerate(chain)]


def _parse_path(data: object, where: str) -> str:
    if not isinstance(data, str) or not data:
        raise ValueError(f"{where} must be a non-empty path")
    return data


def _parse_function(data: object, where: str) -> _Function:
    function = _require_dict(data, where)
    name = function.get("name")
    if not isinstance(name, str) or "." not in name.strip("."):
        raise ValueError(f"{where}.name must be a dotted path")
    args = _require_list(function.get("args", []), f"{where}.args")
    kwargs = _require_dict(function.get("kwargs", {}), f"{where}.kwargs")
    return _Function(name=name, args=args, kwargs=kwargs)


def _require_dict(data: object, where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be a mapping")
    return data  # type: ignore[return-value]


def _require_list(data: object, where: str) -> list[Any]:
    if not isinstance(data, list):
        raise ValueError(f"{where} must be a list")
    return data  # type: ignore[return-value]


def _resolve_source(source: _Source) -> SourceConfig:
    backend_fns = [_resolve_function(_) for _ in source.backend]
    return SourceConfig(
        name=source.name,
        backend=compose_service(backend_fns[0], *backend_fns[1:]),
        cache_path=source.cache,
    )


def _resolve_function(function: _Function) -> Callable[..., Any]:
    base, name = function.name.rsplit(".", 1)
    module = import_module(base)
    fn = getattr(module, name)
    return partial(fn, *function.args, **function.kwargs)

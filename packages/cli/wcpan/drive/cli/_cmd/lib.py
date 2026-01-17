# Need official type support
from argparse import (
    ArgumentParser,
    _SubParsersAction,  # type: ignore
)
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from io import StringIO
from pathlib import PurePath

from wcpan.drive.core.exceptions import UnauthorizedError
from wcpan.drive.core.types import Drive, Node

from .._lib import cout


type SubCommand = _SubParsersAction[ArgumentParser]


def add_help_message(parser: ArgumentParser) -> None:
    sout = StringIO()
    parser.print_help(sout)
    fallback = partial(cout, sout.getvalue())
    parser.set_defaults(action=None, fallback_action=fallback)


def add_bool_argument(
    parser: ArgumentParser,
    name: str,
    *,
    short_true: str | None = None,
    short_false: str | None = None,
) -> None:
    flag = name.replace("_", "-")
    pos_flags = ["--" + flag]
    if short_true:
        pos_flags.append("-" + short_true)
    neg_flags = ["--no-" + flag]
    if short_false:
        neg_flags.append("-" + short_false)
    parser.add_argument(*pos_flags, dest=name, action="store_true")
    parser.add_argument(*neg_flags, dest=name, action="store_false")


def require_authorized[**A](
    fn: Callable[A, Awaitable[int]],
) -> Callable[A, Awaitable[int]]:
    @wraps(fn)
    async def action(*args: A.args, **kwargs: A.kwargs) -> int:
        try:
            return await fn(*args, **kwargs)
        except UnauthorizedError:
            cout("not authorized")
            return 1

    return action


async def for_k_av[T](k: str, v: Awaitable[T]) -> tuple[str, T]:
    return k, await v


async def get_node_by_id_or_path(drive: Drive, id_or_path: str) -> Node:
    if id_or_path[0] == "/":
        node = await drive.get_node_by_path(PurePath(id_or_path))
    else:
        node = await drive.get_node_by_id(id_or_path)
    return node


async def get_path_by_id_or_path(drive: Drive, id_or_path: str) -> PurePath:
    if id_or_path[0] == "/":
        return PurePath(id_or_path)
    node = await drive.get_node_by_id(id_or_path)
    path = await drive.resolve_path(node)
    return path

from argparse import Namespace
from asyncio import as_completed
from pathlib import Path

from wcpan.drive.core.types import Drive

from .._download import download_list
from ..lib import create_executor
from .lib import (
    SubCommand,
    add_bool_argument,
    get_node_by_id_or_path,
    require_authorized,
)


def add_download_command(commands: SubCommand):
    parser = commands.add_parser(
        "download",
        aliases=["dl"],
        help="download files/folders",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=1,
        help="maximum simultaneously download jobs (default: %(default)s)",
    )
    add_bool_argument(parser, "fail_fast")
    add_bool_argument(parser, "include_trash")
    parser.add_argument("id_or_path", type=str, nargs="+")
    parser.add_argument("destination", type=str)
    parser.set_defaults(action=_action_download, fail_fast=True, include_trash=False)


@require_authorized
async def _action_download(drive: Drive, kwargs: Namespace) -> int:
    id_or_path: list[str] = kwargs.id_or_path
    destination: str = kwargs.destination
    jobs: int = kwargs.jobs
    fail_fast: bool = kwargs.fail_fast
    include_trash: bool = kwargs.include_trash

    with create_executor() as pool:
        g = (get_node_by_id_or_path(drive, _) for _ in id_or_path)
        ag = (await _ for _ in as_completed(g))
        node_list = [_ async for _ in ag if not _.is_trashed or include_trash]
        dst = Path(destination)

        ok = await download_list(
            node_list,
            dst,
            drive=drive,
            pool=pool,
            jobs=jobs,
            fail_fast=fail_fast,
            include_trash=include_trash,
        )

    return 0 if ok else 1

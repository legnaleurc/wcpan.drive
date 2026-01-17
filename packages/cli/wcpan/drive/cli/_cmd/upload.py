from argparse import Namespace
from pathlib import Path

from wcpan.drive.core.types import Drive

from .._upload import upload_list
from ..lib import create_executor
from .lib import (
    SubCommand,
    add_bool_argument,
    get_node_by_id_or_path,
    require_authorized,
)


def add_upload_command(commands: SubCommand):
    parser = commands.add_parser(
        "upload",
        aliases=["ul"],
        help="upload files/folders",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=1,
        help="maximum simultaneously upload jobs (default: %(default)s)",
    )
    add_bool_argument(parser, "fail_fast")
    parser.add_argument("source", type=str, nargs="+")
    parser.add_argument("id_or_path", type=str)
    parser.set_defaults(action=_action_upload, fail_fast=True)


@require_authorized
async def _action_upload(drive: Drive, kwargs: Namespace) -> int:
    id_or_path: str = kwargs.id_or_path
    source: list[str] = kwargs.source
    jobs: int = kwargs.jobs
    fail_fast: bool = kwargs.fail_fast

    with create_executor() as pool:
        node = await get_node_by_id_or_path(drive, id_or_path)
        src_list = [Path(_) for _ in source]

        ok = await upload_list(
            src_list, node, drive=drive, pool=pool, jobs=jobs, fail_fast=fail_fast
        )

    return 0 if ok else 1

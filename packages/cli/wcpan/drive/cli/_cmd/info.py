from argparse import Namespace

from wcpan.drive.core.types import Drive

from .._lib import print_as_yaml
from .lib import SubCommand, get_node_by_id_or_path


def add_info_command(commands: SubCommand):
    parser = commands.add_parser(
        "info",
        aliases=["i"],
        help="display file information [offline]",
    )
    parser.set_defaults(action=_action_info)
    parser.add_argument("id_or_path", type=str)


async def _action_info(drive: Drive, kwargs: Namespace) -> int:
    from dataclasses import asdict

    id_or_path: str = kwargs.id_or_path

    node = await get_node_by_id_or_path(drive, id_or_path)
    print_as_yaml(asdict(node))
    return 0

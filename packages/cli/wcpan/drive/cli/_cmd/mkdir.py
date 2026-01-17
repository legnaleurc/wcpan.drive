from argparse import Namespace
from pathlib import PurePath

from wcpan.drive.core.types import Drive

from .lib import SubCommand, require_authorized


def add_mkdir_command(commands: SubCommand):
    parser = commands.add_parser(
        "mkdir",
        help="create folder",
    )
    parser.set_defaults(action=_action_mkdir)
    parser.add_argument("path", type=str)


@require_authorized
async def _action_mkdir(drive: Drive, kwargs: Namespace) -> int:
    path = PurePath(kwargs.path)
    parent_path = path.parent
    name = path.name
    parent_node = await drive.get_node_by_path(parent_path)
    await drive.create_directory(name, parent_node, exist_ok=True)
    return 0

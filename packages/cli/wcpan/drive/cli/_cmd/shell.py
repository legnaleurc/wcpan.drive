from argparse import Namespace

from wcpan.drive.core.exceptions import NodeNotFoundError
from wcpan.drive.core.types import Drive

from .._interaction import interact
from .._lib import cout
from .lib import SubCommand, get_node_by_id_or_path


def add_shell_command(commands: SubCommand):
    parser = commands.add_parser(
        "shell",
        help="start an interactive shell",
    )
    parser.set_defaults(action=_action_shell)
    parser.add_argument("id_or_path", type=str, nargs="?")


async def _action_shell(drive: Drive, args: Namespace) -> int:
    id_or_path: str | None = args.id_or_path

    try:
        node = await get_node_by_id_or_path(drive, id_or_path if id_or_path else "/")
    except NodeNotFoundError:
        cout(f"{id_or_path} does not exist")
        return 1

    if not node.is_directory:
        cout(f"{id_or_path} is not a folder")
        return 1

    interact(drive, node)
    return 0

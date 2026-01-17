from argparse import Namespace

from wcpan.drive.core.types import Drive, Node

from .._lib import cout
from .lib import SubCommand, get_node_by_id_or_path


def add_tree_command(commands: SubCommand):
    tree_parser = commands.add_parser(
        "tree",
        help="recursive list folder [offline]",
    )
    tree_parser.set_defaults(action=_action_tree)
    tree_parser.add_argument("id_or_path", type=str)


async def _action_tree(drive: Drive, kwargs: Namespace) -> int:
    id_or_path: str = kwargs.id_or_path

    node = await get_node_by_id_or_path(drive, id_or_path)
    await _traverse_node(drive, node, 0)
    return 0


async def _traverse_node(drive: Drive, node: Node, level: int) -> None:
    if not node.parent_id:
        _print_node("/", level)
    elif level == 0:
        top_path = await drive.resolve_path(node)
        _print_node(str(top_path), level)
    else:
        _print_node(node.name, level)

    if node.is_directory:
        children = await drive.get_children(node)
        for child in children:
            await _traverse_node(drive, child, level + 1)


def _print_node(name: str, level: int) -> None:
    indention = " " * level
    cout(indention + name)

from argparse import Namespace
from collections.abc import Iterable

from wcpan.drive.core.types import Drive, Node

from .._lib import cout, print_as_yaml
from .lib import SubCommand, add_bool_argument, add_help_message, require_authorized


def add_trash_command(commands: SubCommand):
    parser = commands.add_parser("trash", help="actions for trashes")
    commands = parser.add_subparsers()

    list_parser = commands.add_parser("list", help="list trash [offline]")
    add_bool_argument(list_parser, "flatten")
    list_parser.set_defaults(action=_action_trash_list, flatten=False)

    usage_parser = commands.add_parser(
        "usage", aliases=["df"], help="trash size usage [offline]"
    )
    add_bool_argument(usage_parser, "comma")
    usage_parser.set_defaults(action=_action_trash_usage, comma=True)

    purge_parser = commands.add_parser("purge", aliases=["prune"], help="purge trash")
    add_bool_argument(purge_parser, "ask", short_false="y")
    purge_parser.set_defaults(action=_action_trash_purge, ask=True)

    add_help_message(parser)


async def _action_trash_list(drive: Drive, kwargs: Namespace) -> int:
    flatten: bool = kwargs.flatten

    node_list = await drive.get_trashed_nodes(flatten)
    node_list.sort(key=lambda _: _.mtime)
    rv = [
        {
            "id": _.id,
            "name": _.name,
            "ctime": str(_.ctime),
            "mtime": str(_.mtime),
        }
        for _ in node_list
    ]
    print_as_yaml(rv)
    return 0


async def _action_trash_usage(drive: Drive, kwargs: Namespace) -> int:
    comma: bool = kwargs.comma

    calculator = UsageCalculator(drive)
    node_list = await drive.get_trashed_nodes()
    rv = await calculator(node_list)
    if comma:
        cout(f"{rv:,}")
    else:
        cout(f"{rv}")
    return 0


@require_authorized
async def _action_trash_purge(drive: Drive, kwargs: Namespace) -> int:
    ask: bool = kwargs.ask

    node_list = await drive.get_trashed_nodes()
    count = len(node_list)
    cout(f"Purging {count} items in trash ...")

    if ask:
        answer = input("Are you sure? [y/N]")
        answer = answer.lower()
        if answer != "y":
            cout("Aborted.")
            return 0

    try:
        await drive.purge_trash()
    except Exception as e:
        cout(str(e))
        return 1

    cout("Done.")
    return 0


class UsageCalculator:
    def __init__(self, drive: Drive) -> None:
        self._drive = drive
        self._known: set[str] = set()

    async def __call__(self, node_list: Iterable[Node]) -> int:
        rv = 0
        for node in node_list:
            if node.is_directory:
                children = await self._drive.get_children(node)
                rv += await self(children)
            elif node.id not in self._known:
                rv += node.size
                self._known.add(node.id)
        return rv

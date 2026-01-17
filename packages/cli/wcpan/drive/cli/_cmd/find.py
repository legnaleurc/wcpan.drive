from argparse import Namespace
from asyncio import as_completed

from wcpan.drive.core.types import Drive

from .._lib import cout
from .lib import SubCommand, add_bool_argument, for_k_av


def add_find_command(commands: SubCommand):
    parser = commands.add_parser(
        "find",
        aliases=["f"],
        help="find files/folders by pattern [offline]",
    )
    add_bool_argument(parser, "id_only")
    add_bool_argument(parser, "include_trash")
    parser.add_argument("pattern", type=str)
    parser.set_defaults(action=_action_find, id_only=False, include_trash=False)


async def _action_find(drive: Drive, kwargs: Namespace) -> int:
    pattern: str = kwargs.pattern
    id_only: bool = kwargs.id_only

    nodes = await drive.find_nodes_by_regex(pattern)
    if not kwargs.include_trash:
        nodes = (_ for _ in nodes if not _.is_trashed)

    if id_only:
        for node in nodes:
            cout(node.id)
        return 0

    pairs = [
        await _
        for _ in as_completed(for_k_av(_.id, drive.resolve_path(_)) for _ in nodes)
    ]
    pairs.sort(key=lambda _: _[1])
    for id_, path in pairs:
        cout(f"{id_}: {path}")

    return 0

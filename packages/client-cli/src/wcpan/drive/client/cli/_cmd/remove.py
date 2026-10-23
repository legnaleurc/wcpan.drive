from argparse import Namespace
from asyncio import as_completed
from functools import partial

from wcpan.drive.client.types import Drive

from .._lib import cerr
from .lib import (
    SubCommand,
    add_bool_argument,
    get_node_by_id_or_path,
    require_authenticated,
)


def add_remove_command(commands: SubCommand):
    parser = commands.add_parser(
        "remove",
        aliases=["rm"],
        help="remove files/folders",
    )
    add_bool_argument(parser, "restore")
    add_bool_argument(parser, "purge")
    parser.set_defaults(action=_action_remove, restore=False, purge=False)
    parser.add_argument("id_or_path", type=str, nargs="+")


@require_authenticated
async def _action_remove(drive: Drive, kwargs: Namespace) -> int:
    id_or_path: str = kwargs.id_or_path
    restore: bool = kwargs.restore
    purge: bool = kwargs.purge

    if restore and purge:
        cerr("`--purge` flag conflicts with `--restore`")
        return 1

    action = (
        partial(_purge_node, drive=drive)
        if purge
        else partial(_restore_node if restore else _soft_delete_node, drive=drive)
    )

    rv = 0
    for _ in as_completed(action(_) for _ in id_or_path):
        try:
            await _
        except Exception:
            rv = 1
    return rv


async def _soft_delete_node(id_or_path: str, /, *, drive: Drive) -> None:
    try:
        node = await get_node_by_id_or_path(drive, id_or_path)
    except Exception:
        cerr(f"{id_or_path} does not exist")
        raise

    try:
        await drive.delete(node)
    except Exception as e:
        cerr(f"operation failed on {id_or_path}, reason: {str(e)}")
        raise


async def _restore_node(id_or_path: str, /, *, drive: Drive) -> None:
    try:
        node = await get_node_by_id_or_path(drive, id_or_path)
    except Exception:
        cerr(f"{id_or_path} does not exist")
        raise

    try:
        await drive.restore(node)
    except Exception as e:
        cerr(f"operation failed on {id_or_path}, reason: {str(e)}")
        raise


async def _purge_node(id_or_path: str, /, *, drive: Drive) -> None:
    try:
        node = await get_node_by_id_or_path(drive, id_or_path)
    except Exception:
        cerr(f"{id_or_path} does not exist")
        raise

    try:
        await drive.delete(node, permanent=True)
    except Exception as e:
        cerr(f"operation failed on {id_or_path}, reason: {str(e)}")
        raise

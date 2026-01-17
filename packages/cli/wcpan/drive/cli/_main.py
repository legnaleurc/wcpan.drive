import asyncio
import sys
from argparse import ArgumentParser, Namespace
from logging.config import dictConfig
from pathlib import Path

from wcpan.logging import ConfigBuilder

from . import __version__ as VERSION
from ._cfg import create_drive_from_config
from ._cmd.auth import add_auth_command
from ._cmd.download import add_download_command
from ._cmd.find import add_find_command
from ._cmd.info import add_info_command
from ._cmd.lib import add_help_message
from ._cmd.list_ import add_list_command
from ._cmd.mkdir import add_mkdir_command
from ._cmd.move import add_move_command
from ._cmd.remove import add_remove_command
from ._cmd.shell import add_shell_command
from ._cmd.sync import add_sync_command
from ._cmd.trash import add_trash_command
from ._cmd.tree import add_tree_command
from ._cmd.upload import add_upload_command
from ._cmd.usage import add_usage_command


def main(args: list[str] | None = None) -> int:
    if args is None:
        args = sys.argv
    try:
        return asyncio.run(amain(args[1:]))
    except KeyboardInterrupt:
        return 1


async def amain(args: list[str]) -> int:
    dictConfig(ConfigBuilder().add("wcpan", level="D").to_dict())

    kwargs = _parse_args(args)
    if not kwargs.action:
        kwargs.fallback_action()
        return 0

    config: str = kwargs.config
    path = Path(config)
    async with create_drive_from_config(path) as drive:
        return await kwargs.action(drive, kwargs)


def _parse_args(args: list[str]) -> Namespace:
    parser = ArgumentParser("wcpan.drive.cli")

    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"{VERSION}",
    )

    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help=("specify configuration file path"),
    )

    commands = parser.add_subparsers()

    add_auth_command(commands)
    add_sync_command(commands)
    add_find_command(commands)
    add_info_command(commands)
    add_list_command(commands)
    add_tree_command(commands)
    add_usage_command(commands)
    add_download_command(commands)
    add_upload_command(commands)
    add_remove_command(commands)
    add_move_command(commands)
    add_mkdir_command(commands)
    add_trash_command(commands)
    add_shell_command(commands)

    add_help_message(parser)

    kwargs = parser.parse_args(args)

    return kwargs

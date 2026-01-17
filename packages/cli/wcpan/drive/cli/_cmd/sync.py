from argparse import Namespace
from dataclasses import asdict

from wcpan.drive.core.lib import dispatch_change
from wcpan.drive.core.types import Drive

from .._lib import cout, print_as_yaml
from .lib import SubCommand, add_bool_argument, require_authorized


def add_sync_command(commands: SubCommand):
    parser = commands.add_parser(
        "sync",
        aliases=["s"],
        help="synchronize database",
    )
    add_bool_argument(parser, "verbose", short_true="v")
    parser.set_defaults(action=_action_sync)


@require_authorized
async def _action_sync(drive: Drive, kwargs: Namespace) -> int:
    verbose: bool = kwargs.verbose

    count = 0
    async for change in drive.sync():
        if verbose:
            dispatch_change(
                change,
                on_remove=lambda _: print_as_yaml([_]),
                on_update=lambda _: print_as_yaml([asdict(_)]),
            )
        count += 1
    if not verbose:
        cout(f"{count}")
    return 0

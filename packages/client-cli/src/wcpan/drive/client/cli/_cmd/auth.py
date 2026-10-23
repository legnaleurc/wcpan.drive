from argparse import Namespace

from wcpan.drive.client.types import Drive

from .._lib import cout
from .lib import SubCommand


def add_auth_command(commands: SubCommand):
    parser = commands.add_parser(
        "auth",
        aliases=["a"],
        help="authorize user",
    )
    parser.set_defaults(action=_action_auth)


async def _action_auth(drive: Drive, kwargs: Namespace) -> int:
    try:
        await drive.authenticate()
        cout("Authentication successful")
        return 0
    except Exception as e:
        cout(f"Authentication failed: {e}")
        return 1

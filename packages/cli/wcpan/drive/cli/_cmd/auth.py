from argparse import Namespace

from wcpan.drive.core.types import Drive

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
    url = await drive.get_oauth_url()
    cout("Access the following URL to authorize user:\n")
    cout(url)
    cout("")
    cout("Paste the redireced URL or provided code here:")
    answer = input("")
    await drive.set_oauth_token(answer)
    return 0

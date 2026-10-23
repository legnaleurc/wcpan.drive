from argparse import Namespace

from wcpan.drive.client.config import PLUGIN_GROUPS, create_config_scaffold
from wcpan.drive.plugin import factory_path, list_plugins

from .._lib import cerr, cout
from .lib import SubCommand


def add_init_command(commands: SubCommand):
    parser = commands.add_parser(
        "init",
        help="print a starting config for installed plugins [no config]",
        description="Required settings are left empty to fill in.",
    )
    parser.add_argument(
        "--plugin",
        "-p",
        action="append",
        default=[],
        help="plugin by name, module, or factory path; repeatable",
    )
    parser.add_argument("--list", action="store_true", help="list installed plugins")
    parser.set_defaults(action=None, offline_action=_action_init)


def _action_init(kwargs: Namespace) -> int:
    if kwargs.list:
        for ep in list_plugins(PLUGIN_GROUPS):
            role = ep.group.rsplit(".", 1)[-1]
            cout(f"{ep.name}\t{role}\t{factory_path(ep)}")
        return 0

    try:
        scaffold = create_config_scaffold(kwargs.plugin)
    except ValueError as e:
        cerr(e)
        return 1
    cout(scaffold.removesuffix("\n"))
    return 0

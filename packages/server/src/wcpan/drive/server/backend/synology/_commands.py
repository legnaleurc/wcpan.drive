"""Maintenance commands of a Synology backend instance.

Run as ``wcpan.drive.server --config FILE backend NAME COMMAND ...`` with the
server stopped.
"""

import argparse
from collections.abc import Sequence

from wcpan.drive.server.types import Access, BackendCommand, ComparisonReport


def command_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"wcpan.drive.server backend {name}",
        description=f"Maintenance commands of Synology backend {name!r}.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill = subparsers.add_parser(
        "backfill",
        help="Reconcile node metadata under PATH with Synology Drive",
    )
    backfill.add_argument(
        "path",
        help="Virtual directory path in the mirror (e.g. / for root, /photos/Projects);"
        " only this instance's mounts under it are reconciled",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing the database",
    )
    subparsers.add_parser(
        "pending",
        help="List files withheld because their media could not be probed,"
        " with their Synology Drive paths and why",
    )
    return parser


def parse_command(name: str, argv: Sequence[str]) -> BackendCommand:
    """Parse a command of instance *name*; only a real backfill writes or probes.

    ``pending`` and ``backfill --dry-run`` only read.
    """
    args = command_parser(name).parse_args(list(argv))
    if args.command == "backfill" and not args.dry_run:
        return BackendCommand(access=Access.WRITE, probes=True, args=args)
    return BackendCommand(access=Access.READ, args=args)


def print_stats(report: ComparisonReport) -> None:
    print(
        f"Checked {report.checked} node(s), "
        f"added {report.added}, "
        f"updated {report.updated}, "
        f"removed {report.removed}, "
        f"list errors {report.list_errors}."
    )

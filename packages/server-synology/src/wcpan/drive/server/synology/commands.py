"""Maintenance commands of a Synology backend instance.

Run as ``wcpan.drive.server --config FILE backend NAME COMMAND ...`` with the
server stopped.
"""

import argparse


_STATS = ("checked", "added", "updated", "removed", "list_errors")


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
    return parser


def print_stats(stats: dict[str, int]) -> None:
    print(
        f"Checked {stats['checked']} node(s), "
        f"added {stats['added']}, "
        f"updated {stats['updated']}, "
        f"removed {stats['removed']}, "
        f"list errors {stats['list_errors']}."
    )

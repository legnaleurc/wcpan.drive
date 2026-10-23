"""Maintenance commands of a filesystem backend instance.

Run as ``wcpan.drive.server --config FILE backend NAME COMMAND ...`` with the
server stopped.
"""

import argparse
from collections.abc import Sequence

from wcpan.drive.server.types import Access, BackendCommand, Mounts

from ._handles import filesystem_id
from ._store import FilesystemStore


def command_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"wcpan.drive.server backend {name}",
        description=f"Maintenance commands of filesystem backend {name!r}.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    rebind = subparsers.add_parser(
        "rebind-mount",
        help="Accept that a mount's source now lives on a different filesystem",
    )
    rebind.add_argument("mount", help="Mount name from the config")
    subparsers.add_parser(
        "pending",
        help="List files withheld because their media could not be probed,"
        " with their source paths and why, as of the last full scan",
    )
    return parser


def parse_command(name: str, argv: Sequence[str]) -> BackendCommand:
    """Parse a command of instance *name*; none of them probes media.

    ``pending`` only reads; ``rebind-mount`` writes.
    """
    args = command_parser(name).parse_args(list(argv))
    access = Access.READ if args.command == "pending" else Access.WRITE
    return BackendCommand(access=access, args=args)


async def rebind_mount(
    store: FilesystemStore, mounts: Mounts, mount: str
) -> tuple[str, bool]:
    """Record the filesystem *mount*'s source is on now.

    Returns the filesystem ID and whether it changed; an unchanged one is not
    written again.
    """
    entry = mounts.get(mount)
    if entry is None:
        raise ValueError(f"{mount!r} is not a mount of this backend")
    source = entry.source
    fsid = filesystem_id(source)
    if await store.get_mount_fsid(mount, source) == fsid:
        return fsid, False
    await store.set_mount_fsid(mount, source, fsid)
    return fsid, True

"""Maintenance commands of a filesystem backend instance.

Run as ``wcpan.drive.server --config FILE backend NAME COMMAND ...`` with the
server stopped.
"""

import argparse

from wcpan.drive.server.backend import Mounts

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
    return parser


async def rebind_mount(store: FilesystemStore, mounts: Mounts, mount: str) -> str:
    """Record the filesystem *mount*'s source is on now."""
    entry = mounts.get(mount)
    if entry is None:
        raise ValueError(f"{mount!r} is not a mount of this backend")
    source = entry.source
    fsid = filesystem_id(source)
    await store.set_mount_fsid(mount, source, fsid)
    return fsid

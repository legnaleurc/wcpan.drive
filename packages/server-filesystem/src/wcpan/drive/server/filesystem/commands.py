"""Maintenance commands of a filesystem backend instance.

Run as ``wcpan.drive.server --config FILE backend NAME COMMAND ...`` with the
server stopped.
"""

import argparse
from collections.abc import Mapping

from wcpan.drive.server.services.storage import StorageService

from .handles import filesystem_id
from .store import FilesystemStore


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


async def rebind_mount(
    storage: StorageService, mounts: Mapping[str, str], mount: str
) -> str:
    """Record the filesystem *mount*'s source is on now."""
    source = mounts.get(mount)
    if source is None:
        raise ValueError(f"{mount!r} is not a mount of this backend")
    fsid = filesystem_id(source)
    await FilesystemStore(storage=storage).set_mount_fsid(mount, source, fsid)
    return fsid

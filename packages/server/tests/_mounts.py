"""Mounts with fixed IDs, for tests that build mounts without a mirror."""

from collections.abc import Mapping

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.services.mounts import Mount, Mounts


ROOT_ID = NodeId("root")
"""The fixed ID of the server root in ``make_mounts``."""


def mount_id(name: str) -> NodeId:
    """The fixed ID ``make_mounts`` gives the mount *name*."""
    return NodeId(f"_{name}")


def make_mounts(sources: Mapping[str, str]) -> Mounts:
    """Mounts named by the keys of *sources*, with IDs from ``mount_id``,
    under ``ROOT_ID``."""
    return Mounts(
        (
            Mount(name=name, id=mount_id(name), source=source)
            for name, source in sources.items()
        ),
        root_id=ROOT_ID,
    )

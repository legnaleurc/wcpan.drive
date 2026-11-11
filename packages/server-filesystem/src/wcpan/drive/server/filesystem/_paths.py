"""Host paths of mirrored nodes."""

import os

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.services.mirror import MirrorStore


class NodePaths:
    """Rebuild a node's path from its mirrored ancestors and mount source."""

    def __init__(self, *, mirror: MirrorStore, mounts: Mounts) -> None:
        self._mirror = mirror
        self._mounts = mounts

    @property
    def mounts(self) -> Mounts:
        return self._mounts

    async def path_of(self, node_id: NodeId) -> str | None:
        """``None`` for virtual nodes or a node whose ancestors are incomplete."""
        ancestors = await self._mirror.get_ancestors(node_id)
        mount = self._mounts.of_chain(ancestors)
        if mount is None:
            return None
        return os.path.join(mount.source, *(a.name for a in reversed(ancestors)))

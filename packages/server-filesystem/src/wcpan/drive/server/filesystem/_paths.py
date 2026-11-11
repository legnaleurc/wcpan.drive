"""Host paths of mirrored nodes."""

import os

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.lib.mounts import mount_name
from wcpan.drive.server.services.mirror import MirrorStore


class NodePaths:
    """Rebuild a node's path from its mirrored ancestors and mount source."""

    def __init__(self, *, mirror: MirrorStore, mounts: dict[str, str]) -> None:
        self._mirror = mirror
        self._mounts = mounts

    @property
    def mounts(self) -> dict[str, str]:
        return self._mounts

    async def path_of(self, node_id: NodeId) -> str | None:
        """``None`` for virtual nodes or a node whose ancestors are incomplete."""
        ancestors = await self._mirror.get_ancestors(node_id)
        if not ancestors:
            return None
        top = ancestors[-1]
        name = mount_name(top.parent_id) if top.parent_id is not None else None
        root = self._mounts.get(name) if name is not None else None
        if root is None:
            return None
        return os.path.join(root, *(a.name for a in reversed(ancestors)))

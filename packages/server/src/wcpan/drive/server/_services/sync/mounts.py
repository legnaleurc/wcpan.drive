"""Publishing the core's own nodes: the virtual root and the mounts."""

from ..._types import NodeObservation
from ..mirror.store import MirrorStore
from ..mounts import Mounts, mount_node_records
from .service import NodeSyncService


async def ensure_mount_nodes(
    mounts: Mounts, *, mirror: MirrorStore, node_sync: NodeSyncService
) -> None:
    """Create the root and any missing mount nodes, publishing each new one.

    Existing nodes are left alone, so restarts add no feed rows; a mount added
    to the config appears in the feed. Waits until they are written, since
    backends add children under them.
    """
    records = mount_node_records(mounts)
    existing = await mirror.get_nodes_by_ids([r.id for r in records])
    missing = [r for r in records if r.id not in existing]
    if not missing:
        return
    async with node_sync.observe() as read:
        await node_sync.upsert_batch(
            [NodeObservation(record=r, locator=None) for r in missing], read=read
        )
    await node_sync.flush()

"""Virtual root and mount nodes, owned by the core rather than any backend."""

from collections.abc import Iterable

from ..lib.mounts import SERVER_ROOT_ID, mount_id, structural_records
from ..types import NodeObservation
from .storage import StorageService
from .sync import NodeSyncService


async def ensure_mount_nodes(
    mount_names: Iterable[str], *, storage: StorageService, node_sync: NodeSyncService
) -> None:
    """Create the root and any missing mount nodes, publishing each new one.

    Existing nodes are left alone, so restarts add no feed rows; a mount added
    to the config appears in the feed. Waits until they are written, since
    backends add children under them.
    """
    names = list(mount_names)
    records = structural_records(names)
    ids = [SERVER_ROOT_ID] + [mount_id(name) for name in names]
    existing = await storage.get_nodes_by_ids(ids)
    missing = [r for r in records if r.id not in existing]
    if not missing:
        return
    async with node_sync.observe() as read:
        await node_sync.upsert_batch(
            [NodeObservation(record=r, locator=None) for r in missing], read=read
        )
    await node_sync.flush()

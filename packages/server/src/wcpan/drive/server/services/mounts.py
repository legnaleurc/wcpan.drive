"""Mounts, and the virtual root and mount nodes owned by the core."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..lib.mounts import SERVER_ROOT_ID, mount_id, virtual_directory
from ..types import NodeObservation
from .mirror import MirrorStore
from .sync import NodeSyncService


@dataclass(frozen=True, kw_only=True)
class Mount:
    """A configured mount: its name, the node it appears as, and its source."""

    name: str
    id: NodeId
    # Source location, interpreted by the backend serving the mount.
    source: str


class Mounts(Mapping[str, Mount]):
    """Mounts by name, with lookup by node ID.

    The core builds the full set from the config and hands each backend
    instance the subset it serves. Node IDs of mounts are opaque to backends.
    """

    def __init__(self, mounts: Iterable[Mount] = ()) -> None:
        self._by_name = {m.name: m for m in mounts}
        self._by_id = {m.id: m for m in self._by_name.values()}

    @classmethod
    def from_sources(cls, sources: Mapping[str, str]) -> "Mounts":
        """Mounts named by the keys of *sources*."""
        return cls(
            Mount(name=name, id=mount_id(name), source=source)
            for name, source in sources.items()
        )

    @property
    def root_id(self) -> NodeId:
        """The server root, parent of every mount."""
        return SERVER_ROOT_ID

    def __getitem__(self, name: str) -> Mount:
        return self._by_name[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __repr__(self) -> str:
        return f"Mounts({list(self._by_name.values())!r})"

    def subset(self, names: Iterable[str]) -> "Mounts":
        return Mounts(self._by_name[name] for name in names)

    def by_id(self, node_id: NodeId) -> Mount | None:
        """The mount whose node is *node_id*."""
        return self._by_id.get(node_id)

    def of_chain(self, ancestors: Sequence[NodeRecord]) -> Mount | None:
        """The mount an ancestor chain from ``MirrorStore.get_ancestors`` ends under.

        ``None`` for an empty or detached chain, or one under another mount.
        """
        if not ancestors:
            return None
        parent_id = ancestors[-1].parent_id
        return self.by_id(parent_id) if parent_id is not None else None

    async def containing(self, node_id: NodeId, *, mirror: MirrorStore) -> Mount | None:
        """The mount that is or contains *node_id*, or ``None``."""
        mount = self.by_id(node_id)
        if mount is not None:
            return mount
        return self.of_chain(await mirror.get_ancestors(node_id))


async def ensure_mount_nodes(
    mounts: Mounts, *, mirror: MirrorStore, node_sync: NodeSyncService
) -> None:
    """Create the root and any missing mount nodes, publishing each new one.

    Existing nodes are left alone, so restarts add no feed rows; a mount added
    to the config appears in the feed. Waits until they are written, since
    backends add children under them.
    """
    records = [virtual_directory(mounts.root_id, None, "")] + [
        virtual_directory(m.id, mounts.root_id, m.name) for m in mounts.values()
    ]
    existing = await mirror.get_nodes_by_ids([r.id for r in records])
    missing = [r for r in records if r.id not in existing]
    if not missing:
        return
    async with node_sync.observe() as read:
        await node_sync.upsert_batch(
            [NodeObservation(record=r, locator=None) for r in missing], read=read
        )
    await node_sync.flush()

"""The comparison the ``backfill`` command runs through the core."""

from collections.abc import Sequence

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.types import (
    Comparison,
    Contents,
    MirrorReader,
    NodeObservation,
    ScanPlan,
)
from wcpan.synology.types import SynologyClient

from ._paths import SynologyPathService
from ._store import SynologyStoreReader


class SubtreeComparison(Comparison[NodeId]):
    """This instance's mounts under one mirrored directory, listed in full."""

    def __init__(
        self,
        root_node_id: NodeId,
        *,
        drive_api: SynologyClient,
        mirror: MirrorReader,
        store: SynologyStoreReader,
        syno_paths: SynologyPathService,
    ) -> None:
        self._root_node_id = root_node_id
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._syno_paths = syno_paths

    async def prepare(self) -> ScanPlan[NodeId]:
        mounts = self._syno_paths.mounts
        root_id = self._root_node_id
        if root_id == mounts.root_id:
            children = await self._mirror.get_children(root_id)
            roots = [c.id for c in children if mounts.by_id(c.id) is not None]
        elif await mounts.containing(root_id, mirror=self._mirror) is not None:
            roots = [root_id]
        else:
            roots = []
        return ScanPlan(scope=set(roots), roots=roots, preserved=[])

    def folder_id(self, folder: NodeId) -> NodeId:
        return folder

    async def list_folder(self, folder: NodeId) -> Contents[NodeId]:
        listed = await self._syno_paths.list_for_pass(self._drive_api, folder)
        return Contents(
            children=[o for _, o in listed],
            subfolders=[o.record.id for item, o in listed if item["type"] == "dir"],
        )

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        return await self._syno_paths.locate(self._drive_api, node_id)

    async def differs(
        self, pairs: Sequence[tuple[NodeObservation, NodeRecord]]
    ) -> set[NodeId]:
        locators = await self._store.get_locators([o.record.id for o, _ in pairs])
        return {o.record.id for o, _ in pairs if locators.get(o.record.id) != o.locator}

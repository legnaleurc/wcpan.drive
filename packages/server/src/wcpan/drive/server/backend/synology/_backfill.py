"""Subtree comparison against Synology (CLI ``backfill``), run by the core."""

from collections.abc import Awaitable, Callable, Sequence
from pathlib import PurePosixPath

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.types import (
    Comparison,
    ComparisonReport,
    Contents,
    MirrorReader,
    NodeObservation,
    ScanPlan,
    VirtualPath,
    VirtualPathService,
)
from wcpan.synology.types import SynologyClient

from ._paths import SynologyPathService
from ._store import SynologyStoreReader


type Compare = Callable[..., Awaitable[ComparisonReport]]


class _SubtreeComparison(Comparison[NodeId]):
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
        items = await self._syno_paths.list_for_pass(self._drive_api, folder)
        listed = await self._syno_paths.convert(items, folder)
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


class BackfillService:
    """Compares one subtree with Synology through *compare*.

    *compare* is ``compare`` of the core the command received.
    """

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        mirror: MirrorReader,
        store: SynologyStoreReader,
        syno_paths: SynologyPathService,
        compare: Compare,
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._syno_paths = syno_paths
        self._compare = compare
        self._virtual_paths = VirtualPathService(mirror=mirror)

    async def run_virtual_path(
        self, virtual_path: str, *, dry_run: bool = False
    ) -> ComparisonReport:
        """Resolve *virtual_path* to a directory id and compare that subtree."""
        root_id = await self._virtual_paths.resolve_to_directory_node_id(
            VirtualPath(PurePosixPath(virtual_path))
        )
        return await self._reconcile_subtree(root_id, dry_run=dry_run)

    async def _reconcile_subtree(
        self, root_node_id: NodeId, *, dry_run: bool = False
    ) -> ComparisonReport:
        comparison = _SubtreeComparison(
            root_node_id,
            drive_api=self._drive_api,
            mirror=self._mirror,
            store=self._store,
            syno_paths=self._syno_paths,
        )
        return await self._compare(comparison, dry_run=dry_run)

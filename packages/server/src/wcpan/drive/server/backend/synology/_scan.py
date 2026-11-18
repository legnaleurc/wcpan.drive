"""First-run / resume mirror scan: incremental sync_id BFS, then server-ready signal."""

from dataclasses import dataclass
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.types import (
    Listing,
    MirrorStore,
    NodeObservation,
    NodeSyncService,
    Scanner,
    ScanPlan,
    ScanRunner,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyPermanentLink,
)

from ._paths import SynologyPathService
from ._store import SynologyStore


_L = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Folder:
    node_id: NodeId
    mount: str
    max_id: int = 0
    # New to the mirror, so its contents must be listed whatever its max_id.
    force: bool = False
    is_mount: bool = False


class _MountPass(Scanner[_Folder]):
    """One incremental pass over every mount, each against its own checkpoint.

    A folder whose ``max_id`` is at or below the checkpoint is skipped unless
    the mirror has no children for it or the NAS counts a different number. A
    mount advances only if everything under it was covered.
    """

    # Mounts are scanned one after another.
    concurrent_roots = False

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        mirror: MirrorStore,
        syno_paths: SynologyPathService,
        last_max_ids: dict[str, int],
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._syno_paths = syno_paths
        self._last_max_ids = last_max_ids
        self._highest: dict[str, int] = {}
        self._watermarks: dict[str, int] = {}
        self.per_mount_highest = {
            name: last_max_ids.get(name, 0) for name in syno_paths.mounts
        }
        self.pre_scan_max_ids = dict(self.per_mount_highest)

    def _last(self, mount: str) -> int:
        return self._last_max_ids.get(mount, 0)

    async def prepare(self) -> ScanPlan[_Folder]:
        mounts = self._syno_paths.mounts
        return ScanPlan(
            scope={m.id for m in mounts.values()},
            roots=[
                _Folder(mounts[name].id, name, is_mount=True)
                for name in self._syno_paths.paths
            ],
            preserved=[],
        )

    def folder_id(self, folder: _Folder) -> NodeId:
        return folder.node_id

    async def skip(self, folder: _Folder) -> bool:
        last = self._last(folder.mount)
        if folder.is_mount or folder.force or last <= 0 or folder.max_id > last:
            return False
        children = await self._mirror.get_children(folder.node_id)
        if not children:
            _L.debug(
                "Force-entering folder %s: in DB but no children (max_id=%d)",
                folder.node_id,
                folder.max_id,
            )
            return False
        try:
            _, api_count = await self._drive_api.list_folder(
                await self._syno_paths.permanent_link(folder.node_id),
                offset=0,
                limit=1,
            )
        except Exception:
            _L.warning(
                "Count-check failed for folder %s, preserving subtree", folder.node_id
            )
            return True
        if api_count != len(children):
            _L.debug(
                "Folder %s count mismatch (db=%d api=%d), scanning for deletions",
                folder.node_id,
                len(children),
                api_count,
            )
            return False
        _L.debug(
            "Skipping folder %s (max_id=%d <= last_max_id=%d)",
            folder.node_id,
            folder.max_id,
            last,
        )
        return True

    async def list_folder(self, folder: _Folder) -> Listing[_Folder]:
        mount = folder.mount
        last = self._last(mount)
        _L.debug("Entering folder %s (max_id=%d)", folder.node_id, folder.max_id)
        items = await self._syno_paths.list_children(self._drive_api, folder.node_id)
        listed = await self._syno_paths.convert(items, folder.node_id)
        existing = {n.id for n in await self._mirror.get_children(folder.node_id)}

        if folder.is_mount:
            self._watermarks[mount] = max(
                (item.get("max_id", item.get("sync_id", 0)) for item in items),
                default=last,
            )
        self._highest[mount] = max(
            [
                self._highest.get(mount, last),
                *(item.get("sync_id", 0) for item in items),
            ]
        )

        changed: list[NodeObservation] = []
        subfolders: list[_Folder] = []
        for item, observation in listed:
            sync_id = item.get("sync_id", 0)
            node_id = observation.record.id
            is_new = node_id not in existing
            if sync_id > last or is_new:
                changed.append(observation)
            if item["type"] == "dir":
                max_id = item.get("max_id", sync_id)
                subfolders.append(_Folder(node_id, mount, max_id, is_new))
        return Listing(
            seen=[o.record.id for _, o in listed],
            changed=changed,
            subfolders=subfolders,
            keep=[],
        )

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        return await self._syno_paths.locate(self._drive_api, node_id)

    async def root_finished(self, root: _Folder, complete: bool) -> None:
        if not complete:
            # The next scan must list the uncovered folders again; an advanced
            # checkpoint would let it skip them.
            _L.warning(
                "Mount %r was not fully listed; keeping its checkpoint", root.mount
            )
            return
        last = self._last(root.mount)
        self.per_mount_highest[root.mount] = self._highest.get(root.mount, last)
        self.pre_scan_max_ids[root.mount] = self._watermarks.get(root.mount, last)


class StartupScanService:
    """Aligns nodes and change feed with Synology before ``ready_event`` is set."""

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        mirror: MirrorStore,
        store: SynologyStore,
        syno_paths: SynologyPathService,
        node_sync: NodeSyncService,
        scans: ScanRunner,
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._syno_paths = syno_paths
        self._node_sync = node_sync
        self._scans = scans

    async def _scan_all_mounts(
        self,
        last_max_ids: dict[str, int],
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Incremental sync_id scan across all mounts, then confirmed removals.

        Pass ``last_max_ids`` with zero values for a full initial scan. Returns
        each mount's highest ``sync_id`` seen and its pre-scan watermark.
        """
        scan = _MountPass(
            drive_api=self._drive_api,
            mirror=self._mirror,
            syno_paths=self._syno_paths,
            last_max_ids=last_max_ids,
        )
        await self._scans.run(scan)
        return scan.per_mount_highest, scan.pre_scan_max_ids

    async def _locate_failed(self, source_key: str) -> NodeObservation | None:
        """The file whose media probe failed, by its permanent link."""
        return await self._syno_paths.locate_link(
            self._drive_api, SynologyPermanentLink(source_key)
        )

    async def run_initial_scan(self) -> None:
        """First full scan or resume: align nodes + change feed before serving.

        The root and mount nodes already exist; the core creates them.
        """
        last_max_ids = await self._store.get_mount_max_ids(self._syno_paths.paths)
        if not any(last_max_ids.values()):
            _L.info(
                "First run: performing full scan of %d mount(s)",
                len(self._syno_paths.mounts),
            )
        else:
            _L.info("Resuming with per-mount max_ids: %s", last_max_ids)

        _, pre_scan_max_ids = await self._scan_all_mounts(last_max_ids)
        await self._node_sync.retry_media_failures(locate=self._locate_failed)
        for name, checkpoint in pre_scan_max_ids.items():
            if checkpoint > last_max_ids.get(name, 0):
                await self._node_sync.enqueue(
                    partial(
                        self._store.set_mount_state,
                        name,
                        self._syno_paths.paths[name].path,
                        checkpoint,
                    )
                )
        _L.info("Scan complete; pre-scan checkpoints: %s", pre_scan_max_ids)

    async def run_until_ready(self) -> None:
        """Run the startup scan, and wait for everything it queued."""
        await self.run_initial_scan()
        await self._node_sync.wait_enrichment_drained()
        _L.info("Startup scan applied")

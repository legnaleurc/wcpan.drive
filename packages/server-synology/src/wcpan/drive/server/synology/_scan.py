"""First-run / resume mirror scan: incremental sync_id BFS, then server-ready signal."""

import asyncio
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.lib.bfs import parallel_bfs
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.reconcile import ReconcileSession, reconcile_session
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation, ReadSeq
from wcpan.synology import SynologyClient, SynologyPath

from ._paths import SynologyPathService
from ._store import SynologyStore


_L = getLogger(__name__)


@dataclass
class ScanAccumulator:
    """State for one incremental scan pass."""

    session: ReconcileSession
    highest: int = 0


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
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._syno_paths = syno_paths
        self._node_sync = node_sync

    async def _scan_mount_level(
        self,
        mid: NodeId,
        syno_path: SynologyPath,
        last_max_id: int,
        acc: ScanAccumulator,
        read: ReadSeq,
    ) -> tuple[int, int, list[tuple[NodeId, int, bool]]]:
        """Scan the first level of a mount using the same child listing path service."""
        try:
            items = await self._syno_paths.list_children(self._drive_api, mid)
        except Exception:
            _L.exception("Failed to list mount path %r", syno_path)
            acc.session.preserve(mid)
            return last_max_id, last_max_id, []

        _L.debug("Mount %r: %d item(s) from API", syno_path, len(items))

        db_children = await self._mirror.get_children(mid)
        db_child_ids = {n.id for n in db_children}

        listed = await self._syno_paths.convert(items, mid)
        acc.session.saw(o.record.id for _, o in listed)

        pre_scan_max_id = max(
            (item.get("max_id", item.get("sync_id", 0)) for item in items),
            default=last_max_id,
        )
        highest = max([last_max_id, *(item.get("sync_id", 0) for item in items)])
        subfolders: list[tuple[NodeId, int, bool]] = []
        pending_upserts: list[NodeObservation] = []

        for item, observation in listed:
            sync_id = item.get("sync_id", 0)
            max_id = item.get("max_id", sync_id)
            node_id = observation.record.id
            is_new = node_id not in db_child_ids
            if sync_id > last_max_id or is_new:
                pending_upserts.append(observation)

            if item["type"] == "dir":
                subfolders.append((node_id, max_id, is_new))

        if pending_upserts:
            await self._node_sync.upsert_batch(pending_upserts, read=read)

        _L.debug(
            "Mount %r: %d upsert(s) (deletions deferred to end of scan)",
            syno_path,
            len(pending_upserts),
        )
        return highest, pre_scan_max_id, subfolders

    async def _scan_subtree_bfs(
        self,
        initial: list[tuple[NodeId, int, bool]],
        last_max_id: int,
        acc: ScanAccumulator,
    ) -> None:
        """BFS scan of subfolders using Synology file_ids with max_id pruning."""
        acc.highest = last_max_id

        async def _visit(
            entry: tuple[NodeId, int, bool],
        ) -> list[tuple[NodeId, int, bool]]:
            async with self._node_sync.observe() as read:
                return await _visit_observed(entry, read)

        async def _visit_observed(
            entry: tuple[NodeId, int, bool],
            read: ReadSeq,
        ) -> list[tuple[NodeId, int, bool]]:
            folder_id, this_max_id, force_scan = entry

            if not force_scan and last_max_id > 0 and this_max_id <= last_max_id:
                db_children = await self._mirror.get_children(folder_id)
                if db_children:
                    should_skip = True
                    try:
                        _, api_count = await self._drive_api.list_folder(
                            await self._syno_paths.permanent_link(folder_id),
                            offset=0,
                            limit=1,
                        )
                        if api_count != len(db_children):
                            _L.debug(
                                "Folder %s count mismatch (db=%d api=%d),"
                                " scanning for deletions",
                                folder_id,
                                len(db_children),
                                api_count,
                            )
                            should_skip = False
                    except Exception:
                        _L.warning(
                            "Count-check failed for folder %s, preserving subtree",
                            folder_id,
                        )
                    if should_skip:
                        _L.debug(
                            "Skipping folder %s (max_id=%d <= last_max_id=%d)",
                            folder_id,
                            this_max_id,
                            last_max_id,
                        )
                        acc.session.preserve(folder_id)
                        return []
                else:
                    _L.debug(
                        "Force-entering folder %s: in DB but no children (max_id=%d)",
                        folder_id,
                        this_max_id,
                    )

            _L.debug("Entering folder %s (max_id=%d)", folder_id, this_max_id)
            try:
                items = await self._syno_paths.list_children(self._drive_api, folder_id)
            except Exception:
                _L.exception("Failed to list folder %s", folder_id)
                acc.session.preserve(folder_id)
                return []

            listed = await self._syno_paths.convert(items, folder_id)
            acc.session.saw(o.record.id for _, o in listed)

            db_children = await self._mirror.get_children(folder_id)
            db_child_ids = {n.id for n in db_children}

            pending_dir_upserts: list[NodeObservation] = []
            pending_file_records: list[NodeObservation] = []
            children: list[tuple[NodeId, int, bool]] = []

            acc.highest = max(
                [acc.highest, *(item.get("sync_id", 0) for item in items)]
            )
            for item, observation in listed:
                sync_id = item.get("sync_id", 0)
                max_id = item.get("max_id", sync_id)
                node_id = observation.record.id
                is_new = node_id not in db_child_ids
                if sync_id > last_max_id or is_new:
                    if item["type"] == "dir":
                        pending_dir_upserts.append(observation)
                    else:
                        pending_file_records.append(observation)

                if item["type"] == "dir":
                    children.append((node_id, max_id, is_new))

            if pending_dir_upserts:
                await self._node_sync.upsert_batch(pending_dir_upserts, read=read)
            if pending_file_records:
                await self._node_sync.upsert_file_batch(pending_file_records, read=read)

            return children

        await parallel_bfs(initial, _visit)

    async def _scan_all_mounts(
        self,
        last_max_ids: dict[str, int],
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Incremental sync_id scan across all mounts, then confirmed removals.

        Pass ``last_max_ids`` with zero values for a full initial scan.
        """
        per_mount_highest: dict[str, int] = {
            name: last_max_ids.get(name, 0) for name in self._syno_paths.mounts
        }
        pre_scan_max_ids: dict[str, int] = {
            name: last_max_ids.get(name, 0) for name in self._syno_paths.mounts
        }
        mount_ids = {m.id for m in self._syno_paths.mounts.values()}

        async with reconcile_session(
            mount_ids,
            mirror=self._mirror,
            node_sync=self._node_sync,
            locate=partial(self._syno_paths.locate, self._drive_api),
        ) as session:
            acc = ScanAccumulator(session=session)
            for name, syno_path in self._syno_paths.paths.items():
                _L.debug("Scanning mount %r (%s)", name, syno_path)
                mid = self._syno_paths.mounts[name].id
                mount_last = last_max_ids.get(name, 0)
                try:
                    async with self._node_sync.observe() as level_read:
                        (
                            level_highest,
                            pre_scan_max_id,
                            subfolders,
                        ) = await self._scan_mount_level(
                            mid=mid,
                            syno_path=syno_path,
                            last_max_id=mount_last,
                            acc=acc,
                            read=level_read,
                        )
                    await self._scan_subtree_bfs(
                        initial=subfolders,
                        last_max_id=mount_last,
                        acc=acc,
                    )
                    per_mount_highest[name] = max(
                        mount_last, level_highest, acc.highest
                    )
                    pre_scan_max_ids[name] = pre_scan_max_id
                except Exception:
                    _L.exception("Error scanning mount %r (%s)", name, syno_path)
                    session.preserve(mid)

            await session.commit_removals()

        return per_mount_highest, pre_scan_max_ids

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
        await self._node_sync.retry_pending_enrichment(
            locate=partial(self._syno_paths.locate, self._drive_api)
        )
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

    async def run_until_ready(self, ready_event: asyncio.Event) -> None:
        """Run startup scan then signal ready."""
        await self.run_initial_scan()
        await self._node_sync.wait_enrichment_drained()
        ready_event.set()
        _L.info("Startup scan applied")

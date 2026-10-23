"""First-run / resume mirror scan: incremental sync_id BFS, then server-ready signal."""

import asyncio
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.lib import utc_now
from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.synology import convert_file_info
from wcpan.synology import SynologyClient, SynologyFileId, SynologyPath

from ..lib.bfs import parallel_bfs
from ..lib.mounts import SERVER_ROOT_ID, mount_id
from ..types import NodeObservation, ReadSeq
from .paths import SynologyPathService
from .reconcile import ReconcileSession, reconcile_session
from .storage import StorageService
from .sync import NodeSyncService


_L = getLogger(__name__)


@dataclass
class ScanAccumulator:
    """State for one incremental scan pass."""

    session: ReconcileSession
    highest: int = 0


def _make_root_record() -> NodeRecord:
    now = utc_now()
    return NodeRecord(
        id=SERVER_ROOT_ID,
        parent_id=None,
        name="",
        is_directory=True,
        created_time=now,
        modified_time=now,
        changed_time=now,
        mime_type="application/x-directory",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _make_mount_record(name: str) -> NodeRecord:
    now = utc_now()
    return NodeRecord(
        id=mount_id(name),
        parent_id=SERVER_ROOT_ID,
        name=name,
        is_directory=True,
        created_time=now,
        modified_time=now,
        changed_time=now,
        mime_type="application/x-directory",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _structural_records(mounts: dict[str, SynologyPath]) -> list[NodeRecord]:
    return [_make_root_record()] + [_make_mount_record(name) for name in mounts]


class StartupScanService:
    """Aligns nodes and change feed with Synology before ``ready_event`` is set."""

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        storage: StorageService,
        syno_paths: SynologyPathService,
        node_sync: NodeSyncService,
    ) -> None:
        self._drive_api = drive_api
        self._storage = storage
        self._syno_paths = syno_paths
        self._node_sync = node_sync

    async def _scan_mount_level(
        self,
        mid: MirrorStableId,
        syno_path: SynologyPath,
        last_max_id: int,
        acc: ScanAccumulator,
        read: ReadSeq,
    ) -> tuple[int, int, list[tuple[MirrorStableId, int, bool]]]:
        """Scan the first level of a mount using the same child listing path service."""
        try:
            items = await self._syno_paths.list_children(self._drive_api, mid)
        except Exception:
            _L.exception("Failed to list mount path %r", syno_path)
            acc.session.preserve(mid)
            return last_max_id, last_max_id, []

        _L.debug("Mount %r: %d item(s) from API", syno_path, len(items))

        db_children = await self._storage.get_children(mid)
        db_child_ids = {n.id for n in db_children}

        acc.session.saw(
            o.record.id
            for item in items
            if (o := convert_file_info(item, parent_id=mid)) is not None
        )

        pre_scan_max_id = max(
            (item.get("max_id", item.get("sync_id", 0)) for item in items),
            default=last_max_id,
        )
        highest = last_max_id
        subfolders: list[tuple[MirrorStableId, int, bool]] = []
        pending_upserts: list[NodeObservation] = []

        for item in items:
            sync_id = item.get("sync_id", 0)
            max_id = item.get("max_id", sync_id)
            if sync_id > highest:
                highest = sync_id

            observation = convert_file_info(item, parent_id=mid)
            if observation is None:
                continue
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
        initial: list[tuple[MirrorStableId, int, bool]],
        last_max_id: int,
        acc: ScanAccumulator,
    ) -> None:
        """BFS scan of subfolders using Synology file_ids with max_id pruning."""
        acc.highest = last_max_id

        async def _visit(
            entry: tuple[MirrorStableId, int, bool],
        ) -> list[tuple[MirrorStableId, int, bool]]:
            async with self._node_sync.observe() as read:
                return await _visit_observed(entry, read)

        async def _visit_observed(
            entry: tuple[MirrorStableId, int, bool],
            read: ReadSeq,
        ) -> list[tuple[MirrorStableId, int, bool]]:
            folder_id, this_max_id, force_scan = entry

            if not force_scan and last_max_id > 0 and this_max_id <= last_max_id:
                db_children = await self._storage.get_children(folder_id)
                if db_children:
                    should_skip = True
                    try:
                        locators = await self._storage.get_locators([folder_id])
                        locator = locators.get(folder_id)
                        if locator is None:
                            raise ValueError(f"missing locator for {folder_id}")
                        _, api_count = await self._drive_api.list_folder(
                            SynologyFileId(locator),
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

            acc.session.saw(
                o.record.id
                for item in items
                if (o := convert_file_info(item, parent_id=folder_id)) is not None
            )

            db_children = await self._storage.get_children(folder_id)
            db_child_ids = {n.id for n in db_children}

            pending_dir_upserts: list[NodeObservation] = []
            pending_file_records: list[NodeObservation] = []
            children: list[tuple[MirrorStableId, int, bool]] = []

            for item in items:
                sync_id = item.get("sync_id", 0)
                max_id = item.get("max_id", sync_id)
                if sync_id > acc.highest:
                    acc.highest = sync_id

                observation = convert_file_info(item, parent_id=folder_id)
                if observation is None:
                    continue
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
        mount_ids = {mount_id(n) for n in self._syno_paths.mounts}

        async with reconcile_session(
            mount_ids,
            storage=self._storage,
            node_sync=self._node_sync,
            locate=partial(self._syno_paths.locate, self._drive_api),
        ) as session:
            acc = ScanAccumulator(session=session)
            for name, syno_path in self._syno_paths.mounts.items():
                _L.debug("Scanning mount %r (%s)", name, syno_path)
                mid = mount_id(name)
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
        """First full scan or resume: align nodes + change feed before serving."""
        last_max_ids = await self._storage.get_mount_max_ids(self._syno_paths.mounts)
        is_first_run = not any(last_max_ids.values())

        if is_first_run:
            _L.info(
                "First run: performing full scan of %d mount(s)",
                len(self._syno_paths.mounts),
            )
            _L.debug(
                "Creating root node and %d mount node(s)", len(self._syno_paths.mounts)
            )
            structural = _structural_records(self._syno_paths.mounts)
            async with self._node_sync.observe() as read:
                await self._node_sync.upsert_batch(
                    [NodeObservation(record=r, locator=None) for r in structural],
                    read=read,
                )
        else:
            _L.info("Resuming with per-mount max_ids: %s", last_max_ids)
            # First run already emitted structural rows into `changes`; keep `nodes`
            # in sync (e.g. new mounts in config) without duplicate feed rows.
            structural = _structural_records(self._syno_paths.mounts)
            _L.debug("Ensuring %d mount node(s) exist", len(self._syno_paths.mounts))
            await self._node_sync.sync_nodes(structural)

        _, pre_scan_max_ids = await self._scan_all_mounts(last_max_ids)
        await self._node_sync.retry_pending_enrichment(
            locate=partial(self._syno_paths.locate, self._drive_api)
        )
        for name, checkpoint in pre_scan_max_ids.items():
            if checkpoint > last_max_ids.get(name, 0):
                await self._node_sync.set_mount_watermark(
                    name, self._syno_paths.mounts[name], checkpoint
                )
        _L.info("Scan complete; pre-scan checkpoints: %s", pre_scan_max_ids)

    async def run_until_ready(self, ready_event: asyncio.Event) -> None:
        """Run startup scan then signal ready."""
        await self.run_initial_scan()
        await self._node_sync.wait_enrichment_drained()
        ready_event.set()
        _L.info("Server ready")

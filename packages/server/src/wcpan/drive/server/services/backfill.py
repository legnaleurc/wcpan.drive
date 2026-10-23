"""Subtree reconcile against Synology (CLI ``backfill``); same deps as server scan."""

import logging
from pathlib import PurePosixPath

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.synology import convert_file_info
from wcpan.synology import SynologyClient

from ..lib.bfs import parallel_bfs
from ..lib.mounts import SERVER_ROOT_ID
from ..types import VirtualPath
from .paths import SynologyPathService, VirtualPathService
from .storage import StorageService
from .sync import NodeSyncService


_L = logging.getLogger(__name__)


def _norm_hash(h: str) -> str:
    return h or ""


def _reconcile_api_fields_differ(db: NodeRecord, merged: NodeRecord) -> bool:
    if db.name != merged.name:
        return True
    if db.is_directory != merged.is_directory:
        return True
    if db.size != merged.size:
        return True
    if db.modified_time != merged.modified_time:
        return True
    if db.created_time != merged.created_time:
        return True
    if db.changed_time != merged.changed_time:
        return True
    if _norm_hash(db.hash) != _norm_hash(merged.hash):
        return True
    if db.is_image != merged.is_image:
        return True
    if db.is_video != merged.is_video:
        return True
    if db.mime_type != merged.mime_type:
        return True
    if db.mutable_id != merged.mutable_id:
        return True
    return False


class BackfillService:
    """API-vs-DB subtree reconcile; constructed with the same services as ``StartupScanService``."""

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
        self._virtual_paths = VirtualPathService(storage=storage)

    async def run_virtual_path(
        self, virtual_path: str, *, dry_run: bool = False
    ) -> dict[str, int]:
        """Resolve *virtual_path* to a directory id and reconcile that subtree."""
        root_id = await self._virtual_paths.resolve_to_directory_node_id(
            VirtualPath(PurePosixPath(virtual_path))
        )
        return await self._reconcile_subtree(root_id, dry_run=dry_run)

    async def _reconcile_subtree(
        self,
        root_node_id: MirrorStableId,
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Reconcile: full API vs DB compare per folder; writes via NodeSyncService."""
        cs = self._node_sync

        if root_node_id == SERVER_ROOT_ID:
            roots = await self._storage.get_children(root_node_id)
            initial = [c.id for c in roots]
        else:
            initial = [root_node_id]

        checked = added = updated = removed = list_errors = 0

        async def _visit(parent_id: MirrorStableId) -> list[MirrorStableId]:
            nonlocal checked, added, updated, removed, list_errors
            try:
                items = await self._syno_paths.list_children(self._drive_api, parent_id)
            except Exception:
                _L.exception("Failed to list parent %r", parent_id)
                list_errors += 1
                return []

            db_children = await self._storage.get_children(parent_id)
            db_by_id: dict[MirrorStableId, NodeRecord] = {r.id: r for r in db_children}
            children: list[MirrorStableId] = []

            for item in items:
                checked += 1
                from_api = convert_file_info(item, parent_id=parent_id)
                if from_api is None:
                    continue
                node_id = from_api.id
                existing = db_by_id.get(node_id)

                if existing is None:
                    added += 1
                    _L.info(
                        "Adding missing node %r (%r) under %r",
                        node_id,
                        item["name"],
                        parent_id,
                    )
                    if not dry_run:
                        await cs.reconcile_insert(from_api)
                else:
                    if _reconcile_api_fields_differ(existing, from_api):
                        updated += 1
                        if dry_run:
                            _L.info(
                                "dry-run: would update %r (%r)",
                                node_id,
                                item["name"],
                            )
                        else:
                            await cs.reconcile_update(from_api, existing)

                if item["type"] == "dir":
                    children.append(node_id)

            api_ids = {
                r.id
                for item in items
                if (r := convert_file_info(item, parent_id=parent_id)) is not None
            }
            for node_id, existing in db_by_id.items():
                if node_id not in api_ids:
                    removed += 1
                    _L.info(
                        "Removing node %r (%r) absent from API under %r",
                        node_id,
                        existing.name,
                        parent_id,
                    )
                    if not dry_run:
                        await cs.delete(existing.id)

            return children

        await parallel_bfs(initial, _visit)

        return {
            "checked": checked,
            "added": added,
            "updated": updated,
            "removed": removed,
            "list_errors": list_errors,
        }

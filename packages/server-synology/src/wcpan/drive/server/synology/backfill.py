"""Subtree reconcile against Synology (CLI ``backfill``); same deps as server scan."""

import logging
from functools import partial
from pathlib import PurePosixPath

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib.bfs import parallel_bfs
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID, mount_id, mount_name
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.paths import VirtualPathService
from wcpan.drive.server.services.reconcile import ReconcileSession, reconcile_session
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import ReadSeq, VirtualPath
from wcpan.synology import SynologyClient

from .convert import convert_file_info
from .paths import SynologyPathService
from .store import SynologyStore


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
    if db.hash_algorithm != merged.hash_algorithm:
        return True
    if db.is_image != merged.is_image:
        return True
    if db.is_video != merged.is_video:
        return True
    if db.mime_type != merged.mime_type:
        return True
    return False


class BackfillService:
    """API-vs-DB subtree reconcile; constructed with the same services as ``StartupScanService``."""

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
        self._virtual_paths = VirtualPathService(mirror=mirror)

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
        root_node_id: NodeId,
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Reconcile: full API vs DB compare per folder; writes via NodeSyncService.

        Removals are decided at the end of the pass (see ``ReconcileSession``).
        """
        own = {mount_id(name) for name in self._syno_paths.mounts}
        if root_node_id == SERVER_ROOT_ID:
            roots = await self._mirror.get_children(root_node_id)
            initial = [c.id for c in roots if c.id in own]
        elif await self._mount_of(root_node_id) in own:
            initial = [root_node_id]
        else:
            initial = []
        if not initial:
            return dict.fromkeys(
                ("checked", "added", "updated", "removed", "list_errors"), 0
            )

        async with reconcile_session(
            set(initial),
            mirror=self._mirror,
            node_sync=self._node_sync,
            locate=partial(self._syno_paths.locate, self._drive_api),
        ) as session:
            return await self._reconcile_in_session(
                initial, session=session, dry_run=dry_run
            )

    async def _mount_of(self, node_id: NodeId) -> NodeId | None:
        if mount_name(node_id) is not None:
            return node_id
        ancestors = await self._mirror.get_ancestors(node_id)
        return ancestors[-1].parent_id if ancestors else None

    async def _reconcile_in_session(
        self,
        initial: list[NodeId],
        *,
        session: ReconcileSession,
        dry_run: bool,
    ) -> dict[str, int]:
        cs = self._node_sync
        checked = added = updated = list_errors = 0

        async def _visit(parent_id: NodeId) -> list[NodeId]:
            async with cs.observe() as read:
                return await _visit_observed(parent_id, read)

        async def _visit_observed(parent_id: NodeId, read: ReadSeq) -> list[NodeId]:
            nonlocal checked, added, updated, list_errors
            try:
                items = await self._syno_paths.list_children(self._drive_api, parent_id)
            except Exception:
                _L.exception("Failed to list parent %r", parent_id)
                list_errors += 1
                session.preserve(parent_id)
                return []

            db_children = await self._mirror.get_children(parent_id)
            db_by_id: dict[NodeId, NodeRecord] = {r.id: r for r in db_children}
            db_locators = await self._store.get_locators(list(db_by_id))
            children: list[NodeId] = []

            checked += len(items)
            listed = [
                (item, o)
                for item in items
                if (o := convert_file_info(item, parent_id=parent_id)) is not None
            ]
            session.saw(o.record.id for _, o in listed)

            for item, from_api in listed:
                node_id = from_api.record.id
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
                        await cs.reconcile_insert(from_api, read=read)
                else:
                    if (
                        _reconcile_api_fields_differ(existing, from_api.record)
                        or db_locators.get(node_id) != from_api.locator
                    ):
                        updated += 1
                        if dry_run:
                            _L.info(
                                "dry-run: would update %r (%r)",
                                node_id,
                                item["name"],
                            )
                        else:
                            await cs.reconcile_update(from_api, existing, read=read)

                if item["type"] == "dir":
                    children.append(node_id)

            return children

        await parallel_bfs(initial, _visit)

        if dry_run:
            candidates = await session.removal_roots()
            for node_id in candidates:
                _L.info("dry-run: would remove %r absent from API", node_id)
            removed = len(candidates)
        else:
            removed_ids = await session.commit_removals()
            for node_id in removed_ids:
                _L.info("Removed node %r absent from API", node_id)
            removed = len(removed_ids)

        return {
            "checked": checked,
            "added": added,
            "updated": updated,
            "removed": removed,
            "list_errors": list_errors,
        }

"""Subtree reconcile against Synology (CLI ``backfill``); same deps as server scan."""

import logging
from functools import partial
from pathlib import PurePosixPath

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib import parallel_bfs, reconcile_session
from wcpan.drive.server.types import (
    MirrorReader,
    NodeOrdering,
    NodeSyncService,
    ReadSeq,
    ReconcileSession,
    VirtualPath,
    VirtualPathService,
)
from wcpan.synology.types import SynologyClient

from ._paths import SynologyPathService
from ._store import SynologyStoreReader


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


def _dimensions_missing(db: NodeRecord, merged: NodeRecord) -> bool:
    """A published image or video without complete dimensions.

    Files published before their media could be probed, such as while a local
    path for probing was unreachable.
    """
    return (merged.is_image or merged.is_video) and (db.width <= 0 or db.height <= 0)


class BackfillService:
    """API-vs-DB subtree reconcile; constructed with the same services as ``StartupScanService``.

    One traversal serves both modes: listing, conversion, and comparison need
    only the read side, and the writes (inserts, updates, confirmed removals)
    happen only when *node_sync* is a ``NodeSyncService`` and the run is not a
    dry run. A dry run can therefore pass the read ordering alone.
    """

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        mirror: MirrorReader,
        store: SynologyStoreReader,
        syno_paths: SynologyPathService,
        node_sync: NodeSyncService | NodeOrdering,
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._syno_paths = syno_paths
        self._node_sync = node_sync
        self._virtual_paths = VirtualPathService(mirror=mirror)

    def _require_writer(self) -> NodeSyncService:
        if not isinstance(self._node_sync, NodeSyncService):
            raise RuntimeError("only a dry run can reconcile without a writer")
        return self._node_sync

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
        mounts = self._syno_paths.mounts
        if root_node_id == mounts.root_id:
            roots = await self._mirror.get_children(root_node_id)
            initial = [c.id for c in roots if mounts.by_id(c.id) is not None]
        elif await mounts.containing(root_node_id, mirror=self._mirror) is not None:
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

    async def _reconcile_in_session(
        self,
        initial: list[NodeId],
        *,
        session: ReconcileSession,
        dry_run: bool,
    ) -> dict[str, int]:
        ordering = self._node_sync
        writer = None if dry_run else self._require_writer()
        checked = added = updated = list_errors = 0

        async def _visit(parent_id: NodeId) -> list[NodeId]:
            async with ordering.observe() as read:
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
            listed = await self._syno_paths.convert(items, parent_id)
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
                    if writer is not None:
                        await writer.reconcile_insert(from_api, read=read)
                else:
                    if (
                        _reconcile_api_fields_differ(existing, from_api.record)
                        or db_locators.get(node_id) != from_api.locator
                        or _dimensions_missing(existing, from_api.record)
                    ):
                        updated += 1
                        if writer is None:
                            _L.info(
                                "dry-run: would update %r (%r)",
                                node_id,
                                item["name"],
                            )
                        else:
                            await writer.reconcile_update(from_api, existing, read=read)

                if item["type"] == "dir":
                    children.append(node_id)

            return children

        await parallel_bfs(initial, _visit)

        if writer is None:
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

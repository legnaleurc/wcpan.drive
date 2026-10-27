"""Webhook event processing service with debounced file upsert handling."""

import asyncio
from dataclasses import dataclass
from logging import getLogger

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib.bfs import parallel_bfs
from wcpan.drive.server.lib.debounce import TaskIdDebouncer
from wcpan.drive.server.lib.mounts import mount_name
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation, ReadSeq
from wcpan.synology import (
    SynologyClient,
    SynologyFileId,
    SynologyPermanentLink,
    SynologyWebhookEvent,
)

from .convert import SynologyLocator, convert_file_info
from .mounts import MountRegistry
from .paths import SynologyPathService
from .store import SynologyStore


_L = getLogger(__name__)

type WebhookQueue = asyncio.Queue[SynologyWebhookEvent]


def create_webhook_queue() -> WebhookQueue:
    return asyncio.Queue()


_PENDING_FILE_DELAY = 10.0


@dataclass(frozen=True, slots=True)
class _WebhookActionPlan:
    event_type: str
    schedule_key: str
    file_ref: SynologyFileId
    permanent_link_ref: SynologyPermanentLink
    parent_file_ref: SynologyFileId | None
    fetch_immediately: bool = False
    wait_for_writes: bool = False
    scan_moved_dir_subtree: bool = False
    delete_id: NodeId | None = None
    schedule_delayed_upsert: bool = False


class WebhookService:
    """Processes Synology webhook events as a background consumer."""

    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        mirror: MirrorStore,
        store: SynologyStore,
        node_sync: NodeSyncService,
        syno_paths: SynologyPathService,
        mount_registry: MountRegistry,
    ) -> None:
        self._drive_api = drive_api
        self._mirror = mirror
        self._store = store
        self._node_sync = node_sync
        self._syno_paths = syno_paths
        self._mount_registry = mount_registry

    async def _fetch_and_enrich(
        self,
        file_ref: SynologyFileId,
        permanent_link_ref: SynologyPermanentLink,
        parent_file_ref: SynologyFileId | None,
        event_type: str,
    ) -> None:
        async with self._node_sync.observe() as read:
            await self._fetch_and_apply(
                file_ref, permanent_link_ref, parent_file_ref, event_type, read=read
            )

    async def _fetch_and_apply(
        self,
        file_ref: SynologyFileId,
        permanent_link_ref: SynologyPermanentLink,
        parent_file_ref: SynologyFileId | None,
        event_type: str,
        *,
        read: ReadSeq,
    ) -> None:
        if parent_file_ref is None:
            _L.warning(
                "file_id %s %s without parent_id; skipping",
                file_ref.file_id,
                event_type,
            )
            return
        parent_node = await self._store.get_node_by_locator(
            SynologyLocator(parent_file_ref.file_id)
        )
        if parent_node is None:
            resolved = self._mount_registry.lookup_mount_virtual_id(parent_file_ref)
            if resolved is None:
                if event_type in ("file_moved", "file_renamed"):
                    existing = await self._mirror.get_node_by_id(
                        NodeId(permanent_link_ref.permanent_link)
                    )
                    if existing is not None:
                        _L.debug(
                            "file_id %s moved outside configured mounts; deleting from mirror",
                            file_ref.file_id,
                        )
                        await self._node_sync.delete(existing.id, read=read)
                else:
                    _L.warning(
                        "parent_id %s for %s (%s) not in DB and not a known mount; skipping",
                        parent_file_ref.file_id,
                        file_ref.file_id,
                        event_type,
                    )
                return
            effective_parent_id = resolved
        else:
            effective_parent_id = parent_node.id
        info = await self._drive_api.get_file(permanent_link_ref)
        if not info:
            _L.warning(
                "%s not found after %s; skipping",
                permanent_link_ref,
                event_type,
            )
            return
        observation = convert_file_info(info, effective_parent_id)
        if observation is None:
            return
        if observation.record.id != permanent_link_ref.permanent_link:
            _L.debug(
                "Webhook permanent_link mismatch for file_id %s: payload=%s fetched=%s",
                file_ref.file_id,
                permanent_link_ref.permanent_link,
                observation.record.id,
            )
        await self._node_sync.upsert(observation, read=read)

    def _classify_webhook_item(
        self,
        item: SynologyWebhookEvent,
    ) -> _WebhookActionPlan:
        """Classify one webhook event into an execution plan."""
        event_type = item["event_type"]
        file_id = item["file_id"]
        permanent_link = item["permanent_link"]
        file_type = item["file_type"]
        parent_id = item["parent_id"]

        permanent_link_ref = SynologyPermanentLink(permanent_link=permanent_link)
        file_ref = SynologyFileId(file_id=file_id)
        parent_file_ref = SynologyFileId(file_id=parent_id) if parent_id else None

        if event_type == "file_removed":
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
                delete_id=NodeId(permanent_link_ref.permanent_link),
            )

        if event_type == "file_modified" and file_type == "file":
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
                schedule_delayed_upsert=True,
            )

        if event_type == "file_modified":
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
            )

        if event_type == "file_created" and file_type == "dir":
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
                fetch_immediately=True,
                wait_for_writes=True,
            )

        if event_type in ("file_moved", "file_renamed"):
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
                fetch_immediately=True,
                wait_for_writes=event_type == "file_moved" and file_type == "dir",
                scan_moved_dir_subtree=event_type == "file_moved"
                and file_type == "dir",
            )

        if event_type == "file_created" and file_type == "file":
            return _WebhookActionPlan(
                event_type=event_type,
                schedule_key=permanent_link,
                file_ref=file_ref,
                permanent_link_ref=permanent_link_ref,
                parent_file_ref=parent_file_ref,
                schedule_delayed_upsert=True,
            )

        return _WebhookActionPlan(
            event_type=event_type,
            schedule_key=permanent_link,
            file_ref=file_ref,
            permanent_link_ref=permanent_link_ref,
            parent_file_ref=parent_file_ref,
        )

    async def _execute_webhook_plan(
        self,
        plan: _WebhookActionPlan,
        *,
        pending: TaskIdDebouncer,
    ) -> None:
        skip_moved_dir_subtree_scan = await self._can_skip_moved_dir_subtree_scan(plan)
        if plan.fetch_immediately:
            await self._fetch_and_enrich(
                plan.file_ref,
                plan.permanent_link_ref,
                plan.parent_file_ref,
                plan.event_type,
            )
        if plan.wait_for_writes:
            await self._node_sync.flush()
        if plan.scan_moved_dir_subtree and not skip_moved_dir_subtree_scan:
            subtree_root_id = await self._resolve_moved_dir_root_id(
                plan.file_ref,
                plan.permanent_link_ref,
            )
            if subtree_root_id is None:
                _L.warning(
                    "Skipping moved-dir subtree scan for unresolved root file_id=%s permanent_link=%s",
                    plan.file_ref.file_id,
                    plan.permanent_link_ref.permanent_link,
                )
            else:
                await self._scan_moved_dir_subtree(subtree_root_id)
        if plan.delete_id is not None:
            async with self._node_sync.observe() as read:
                await self._node_sync.delete(plan.delete_id, read=read)
        if plan.schedule_delayed_upsert:
            pending.schedule(
                plan.schedule_key,
                lambda: self._fetch_and_enrich(
                    plan.file_ref,
                    plan.permanent_link_ref,
                    plan.parent_file_ref,
                    plan.event_type,
                ),
            )

    async def _can_skip_moved_dir_subtree_scan(
        self,
        plan: _WebhookActionPlan,
    ) -> bool:
        if not plan.scan_moved_dir_subtree:
            return False

        source_node = await self._mirror.get_node_by_id(
            NodeId(plan.permanent_link_ref.permanent_link)
        )
        if source_node is None:
            source_node = await self._store.get_node_by_locator(
                SynologyLocator(plan.file_ref.file_id)
            )
        if source_node is None:
            return False
        if not await self._is_node_under_configured_mount(source_node):
            return False

        parent_ref = plan.parent_file_ref
        if parent_ref is None:
            return False
        destination_parent = await self._store.get_node_by_locator(
            SynologyLocator(parent_ref.file_id)
        )
        if destination_parent is not None:
            return await self._is_node_under_configured_mount(destination_parent)
        return self._mount_registry.lookup_mount_virtual_id(parent_ref) is not None

    async def _is_node_under_configured_mount(self, node: NodeRecord) -> bool:
        if node.parent_id is None:
            return False

        direct_mount = mount_name(node.parent_id)
        if direct_mount is not None:
            return direct_mount in self._mount_registry.mounts

        ancestors = await self._mirror.get_ancestors(node.id)
        if not ancestors:
            return False
        top = ancestors[-1]
        if top.parent_id is None:
            return False
        ancestor_mount = mount_name(top.parent_id)
        return ancestor_mount in self._mount_registry.mounts

    async def _scan_moved_dir_subtree(self, folder_id: NodeId) -> None:
        """BFS-populate children of a moved directory not delivered by the webhook."""

        async def _visit(current_id: NodeId) -> list[NodeId]:
            async with self._node_sync.observe() as read:
                return await _visit_observed(current_id, read)

        async def _visit_observed(current_id: NodeId, read: ReadSeq) -> list[NodeId]:
            try:
                items = await self._syno_paths.list_children(
                    self._drive_api, current_id
                )
            except Exception:
                _L.exception(
                    "moved-dir scan: failed to list children of %s", current_id
                )
                return []
            dir_records: list[NodeObservation] = []
            file_records: list[NodeObservation] = []
            children: list[NodeId] = []
            for item in items:
                observation = convert_file_info(item, parent_id=current_id)
                if observation is None:
                    continue
                if item["type"] == "dir":
                    dir_records.append(observation)
                    children.append(observation.record.id)
                else:
                    file_records.append(observation)
            if dir_records:
                await self._node_sync.upsert_batch(dir_records, read=read)
                await self._node_sync.flush()
            if file_records:
                await self._node_sync.upsert_file_batch(file_records, read=read)
            return children

        await parallel_bfs([folder_id], _visit)

    async def _resolve_moved_dir_root_id(
        self,
        file_ref: SynologyFileId,
        permanent_link_ref: SynologyPermanentLink,
    ) -> NodeId | None:
        record = await self._mirror.get_node_by_id(
            NodeId(permanent_link_ref.permanent_link)
        )
        if record is not None:
            return record.id
        info = await self._drive_api.get_file(permanent_link_ref)
        if info is not None:
            permanent_link = info.get("permanent_link")
            return NodeId(permanent_link or permanent_link_ref.permanent_link)
        record = await self._store.get_node_by_locator(
            SynologyLocator(file_ref.file_id)
        )
        if record is not None:
            return record.id
        return None

    async def run(
        self,
        queue: WebhookQueue,
        group: asyncio.TaskGroup,
        scan_done_event: asyncio.Event,
    ) -> None:
        pending = TaskIdDebouncer(group, _PENDING_FILE_DELAY)
        await scan_done_event.wait()
        while True:
            item = await queue.get()
            _L.debug("webhook %r", item)
            try:
                plan = self._classify_webhook_item(item)
                await self._execute_webhook_plan(plan, pending=pending)
            except Exception:
                _L.exception("Failed to process webhook item %s; ignoring", item)
            finally:
                queue.task_done()

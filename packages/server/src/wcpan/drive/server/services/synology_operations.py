"""Synology Drive implementation of ``NodeOperations``."""

from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from dataclasses import replace
from logging import getLogger

from aiohttp.client_exceptions import ClientConnectionResetError
from wcpan.drive.protocol.lib import guess_mime_type, utc_now
from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.synology import SYNOLOGY_HASH_ALGORITHM, convert_file_info
from wcpan.synology import (
    SynologyClient,
    SynologyNetworkError,
    SynologyPermanentLink,
    SynologyUploadConflictError,
)

from ..types import NodeObservation, SynologyLocator
from .operations import OperationConflictError, OperationUnavailableError
from .paths import SynologyPathService
from .sync import NodeSyncService


_L = getLogger(__name__)


def _stable_node_ref(record: NodeRecord) -> SynologyPermanentLink:
    return SynologyPermanentLink(str(record.id))


class SynologyNodeOperations:
    def __init__(
        self,
        *,
        drive_api: SynologyClient,
        syno_paths: SynologyPathService,
        node_sync: NodeSyncService,
    ) -> None:
        self._drive_api = drive_api
        self._syno_paths = syno_paths
        self._node_sync = node_sync

    async def create_folder(self, parent_id: MirrorStableId, name: str) -> NodeRecord:
        async with self._node_sync.observe() as read:
            parent_ref = await self._syno_paths.synology_parent_ref(parent_id)
            try:
                info = await self._drive_api.create_folder(parent_ref, name)
            except SynologyUploadConflictError as e:
                record = await self._upsert_existing_child(parent_id, name)
                raise OperationConflictError(
                    f"{name!r} already exists in this folder", record=record
                ) from e

            permanent_link = info.get("permanent_link")
            if not permanent_link:
                _L.warning(
                    "create_folder response missing permanent_link for file_id=%s; aborting",
                    info["file_id"],
                )
                raise OperationUnavailableError(
                    "Synology did not return permanent_link for new folder"
                )
            record = NodeRecord(
                id=MirrorStableId(permanent_link),
                parent_id=parent_id,
                name=info["name"],
                is_directory=True,
                created_time=info.get("created_time", 0),
                modified_time=info.get("modified_time", 0),
                changed_time=info.get("change_time", 0),
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
            return await self._node_sync.upsert(
                NodeObservation(
                    record=record, locator=SynologyLocator(info["file_id"])
                ),
                read=read,
            )

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: MirrorStableId | None,
    ) -> NodeRecord:
        async with self._node_sync.observe() as read:
            updated = record
            locator: SynologyLocator | None = None

            if name and name != record.name:
                try:
                    info = await self._drive_api.rename(_stable_node_ref(updated), name)
                except SynologyUploadConflictError as e:
                    raise OperationConflictError(
                        f"{name!r} already exists in this folder"
                    ) from e
                hash_ = updated.hash
                hash_algorithm = updated.hash_algorithm
                if not updated.is_directory and "hash" in info:
                    hash_ = info["hash"]
                    hash_algorithm = SYNOLOGY_HASH_ALGORITHM if hash_ else ""
                updated = replace(
                    updated,
                    name=info["name"],
                    created_time=info.get("created_time", 0) or updated.created_time,
                    modified_time=info.get("modified_time", 0) or updated.modified_time,
                    changed_time=info.get("change_time", 0) or updated.changed_time,
                    mime_type=guess_mime_type(
                        info["name"], is_directory=updated.is_directory
                    ),
                    hash=hash_,
                    hash_algorithm=hash_algorithm,
                    size=info.get("size", updated.size),
                )
                if raw := info.get("file_id"):
                    locator = SynologyLocator(raw)

            if parent_id and parent_id != record.parent_id:
                parent_ref = await self._syno_paths.synology_parent_ref(parent_id)
                try:
                    await self._drive_api.move(_stable_node_ref(updated), parent_ref)
                except Exception as e:
                    raise OperationUnavailableError(str(e)) from e
                actual = await self._resolve_after_move(updated, parent_id)
                if actual is None:
                    updated = replace(
                        updated, parent_id=parent_id, changed_time=utc_now()
                    )
                else:
                    updated = actual.record
                    locator = actual.locator
                    if updated.id != record.id:
                        await self._node_sync.delete(record.id, read=read)

            return await self._node_sync.upsert(
                NodeObservation(record=updated, locator=locator), read=read
            )

    async def delete(self, record: NodeRecord) -> None:
        async with self._node_sync.observe() as read:
            await self._drive_api.delete(_stable_node_ref(record))
            await self._node_sync.delete(record.id, read=read)

    @asynccontextmanager
    async def download(
        self, record: NodeRecord, *, range_: slice | None
    ) -> AsyncGenerator[AsyncIterable[bytes], None]:
        try:
            async with self._drive_api.download(
                _stable_node_ref(record), range_=range_
            ) as response:
                yield response.content.iter_any()
        except SynologyNetworkError as e:
            original = e.original_error
            if isinstance(original, (ConnectionError, ClientConnectionResetError)):
                # A client disconnect, wrapped by drive_api.download.
                raise original from e
            raise OperationUnavailableError(str(e)) from e

    async def _upsert_existing_child(
        self, parent_id: MirrorStableId, name: str
    ) -> NodeRecord:
        """Mirror the node that holds *name* under *parent_id* (conflict recovery)."""
        async with self._node_sync.observe() as read:
            info = await self._syno_paths.find_child_by_name(
                self._drive_api, parent_id, name
            )
            if info is None:
                raise OperationConflictError(
                    f"Name conflict for {name!r} but existing node could not be resolved"
                )
            observation = convert_file_info(info, parent_id)
            if observation is None:
                raise OperationUnavailableError(
                    "Synology API response missing permanent_link"
                )
            return await self._node_sync.upsert(observation, read=read)

    async def _resolve_after_move(
        self, record: NodeRecord, parent_id: MirrorStableId
    ) -> NodeObservation | None:
        info = await self._drive_api.get_file(_stable_node_ref(record))
        if info is None:
            info = await self._syno_paths.find_child_by_name(
                self._drive_api, parent_id, record.name
            )
        if info is None:
            return None
        return convert_file_info(info, parent_id)

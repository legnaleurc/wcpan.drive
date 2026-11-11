"""Synology Drive implementation of the upload ``Uploader``."""

from collections.abc import AsyncIterable
from dataclasses import replace
from logging import getLogger

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.lib.names import normalize_name
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadConflictError,
    UploadNameTooLongError,
    UploadPermanentError,
    UploadTransientError,
)
from wcpan.drive.server.types import ReadSeq
from wcpan.synology import (
    SynologyClient,
    SynologyFileInfo,
    SynologyNameTooLongError,
    SynologyNetworkError,
    SynologyUploadConflictError,
    SynologyUploadError,
)

from ._paths import SynologyPathService


_L = getLogger(__name__)


class SynologyUploader:
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

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        existing = await self._syno_paths.find_child_by_name(
            self._drive_api, parent_id, name
        )
        if existing is None:
            return None
        conflict = await self._syno_paths.convert_one(existing, parent_id)
        if conflict is None:
            _L.warning(
                "Conflict detected for %r under %r but existing node lacks permanent_link; proceeding",
                name,
                parent_id,
            )
            return None
        return conflict.record

    async def upload(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        parent_ref = await self._syno_paths.synology_parent_ref(parent_id)
        async with self._node_sync.observe() as read:
            try:
                info = await self._drive_api.upload(
                    parent_path=parent_ref,
                    name=name,
                    data=content,
                    mime_type=mime_type,
                )
            except SynologyUploadConflictError:
                record = await self._resolve_name_conflict(
                    parent_id=parent_id,
                    name=name,
                )
                raise UploadConflictError(record)
            except SynologyNetworkError as e:
                raise UploadTransientError(str(e)) from e
            except SynologyNameTooLongError as e:
                raise UploadNameTooLongError(e.file_name or name) from e
            except SynologyUploadError as e:
                raise UploadPermanentError(str(e)) from e
            return await self._upsert_uploaded_node(
                info=info,
                parent_id=parent_id,
                media_info=media_info,
                read=read,
            )

    async def _resolve_name_conflict(
        self,
        *,
        parent_id: NodeId,
        name: str,
    ) -> NodeRecord:
        name = normalize_name(name)
        info = await self._syno_paths.find_child_by_name(
            self._drive_api,
            parent_id,
            name,
        )
        if info is None:
            raise SynologyUploadConflictError(
                f"Name conflict for {name!r} but existing node could not be resolved",
                file_name=name,
            )
        observation = await self._syno_paths.convert_one(info, parent_id)
        if observation is None:
            raise SynologyUploadConflictError(
                f"Name conflict for {name!r} but existing node lacks permanent_link",
                file_name=name,
            )
        return observation.record

    async def _upsert_uploaded_node(
        self,
        *,
        info: SynologyFileInfo,
        parent_id: NodeId,
        media_info: MediaInfo | None,
        read: ReadSeq,
    ) -> NodeRecord:
        observation = await self._syno_paths.convert_one(info, parent_id)
        if observation is None:
            raise SynologyUploadError(
                f"Uploaded node {info.get('name', '?')!r} lacks permanent_link in API response",
                file_name=info.get("name", ""),
            )
        if media_info is not None:
            record = replace(
                observation.record,
                is_image=media_info.is_image,
                is_video=media_info.is_video,
                width=media_info.width,
                height=media_info.height,
                ms_duration=media_info.ms_duration,
            )
            observation = replace(observation, record=record)
        return await self._node_sync.upsert(observation, read=read)

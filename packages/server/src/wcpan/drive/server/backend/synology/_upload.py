"""Synology Drive implementation of the upload ``Uploader``."""

from collections.abc import AsyncIterable
from dataclasses import replace
from logging import getLogger

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.exceptions import (
    MediaEnrichmentError,
    UploadConflictError,
    UploadInvalidMediaError,
    UploadTransientError,
)
from wcpan.drive.server.lib import normalize_name
from wcpan.drive.server.types import NodeSyncService, ProbeFailure, ReadSeq
from wcpan.synology.exceptions import (
    SynologyApiError,
    SynologyError,
    SynologyNetworkError,
    SynologyUploadConflictError,
    SynologyUploadError,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyFileInfo,
    SynologyPermanentLink,
)

from ._errors import upload_error
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
        # A missing child is the expected answer; a missing parent is not.
        try:
            existing = await self._syno_paths.find_child_by_name(
                self._drive_api, parent_id, name
            )
        except SynologyNetworkError as e:
            raise UploadTransientError(str(e)) from e
        except SynologyApiError as e:
            raise upload_error(e, name) from e
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
            except SynologyApiError as e:
                raise upload_error(e, name) from e
            try:
                return await self._upsert_uploaded_node(
                    info=info,
                    parent_id=parent_id,
                    media_info=media_info,
                    read=read,
                )
            except MediaEnrichmentError as e:
                # Only when the Drive classifies as media a file whose name
                # did not say so; anything else was probed before uploading.
                if e.kind != ProbeFailure.INVALID:
                    # Kept: its webhook observes it like any other new file.
                    raise UploadTransientError(
                        f"Cannot probe the media of {name!r} now: {e.reason}"
                    ) from e
                await self._remove_invalid(info)
                raise UploadInvalidMediaError(
                    f"Cannot read the media of {name!r}"
                ) from e

    async def _remove_invalid(self, info: SynologyFileInfo) -> None:
        """Delete an uploaded file whose media could not be read.

        A file left behind by a failed delete is withheld like any other file
        whose probe failed.
        """
        link = info.get("permanent_link")
        if not link:
            _L.warning(
                "Cannot delete invalid upload %r: no permanent link", info["name"]
            )
            return
        try:
            await self._drive_api.delete(SynologyPermanentLink(permanent_link=link))
        except SynologyError as e:
            _L.warning("Cannot delete invalid upload %r: %s", info["name"], e)

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
            )
            observation = replace(observation, record=record)
        return await self._node_sync.upsert(observation, read=read, strict=True)

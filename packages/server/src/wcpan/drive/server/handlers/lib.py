"""Shared utilities for handler modules."""

import functools
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace

from aiohttp import web
from wcpan.drive.protocol.lib import NodeRecordDict, node_record_to_dict
from wcpan.drive.protocol.types import MediaInfo, MirrorStableId, NodeRecord
from wcpan.drive.server.synology import convert_file_info
from wcpan.synology import SynologyFileInfo

from ..keys import (
    CHANGE_SERVICE_KEY,
    READY_KEY,
    SYNOLOGY_DRIVE_API_KEY,
    SYNOLOGY_PATH_KEY,
)
from ..services.sync import NodeSyncService


_MEDIA_QUERY_KEYS = ("width", "height", "ms_duration", "media_image", "media_video")


def media_info_from_query(q: Mapping[str, str]) -> MediaInfo | None:
    if not any(k in q for k in _MEDIA_QUERY_KEYS):
        return None
    return MediaInfo(
        width=int(q.get("width", 0)),
        height=int(q.get("height", 0)),
        ms_duration=int(q.get("ms_duration", 0)),
        is_image=q.get("media_image", "") in ("1", "true"),
        is_video=q.get("media_video", "") in ("1", "true"),
    )


def record_to_response(record: NodeRecord) -> NodeRecordDict:
    return node_record_to_dict(record)


def require_ready(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        if not request.app[READY_KEY]:
            raise web.HTTPServiceUnavailable(reason="Server not ready")
        return await handler(request)

    return wrapper


async def enrich_and_upsert_synology_node(
    *,
    info: SynologyFileInfo,
    parent_id: MirrorStableId,
    node_sync: NodeSyncService,
    media_info: MediaInfo | None = None,
) -> NodeRecord:
    observation = convert_file_info(info, parent_id)
    if observation is None:
        raise web.HTTPServiceUnavailable(
            reason="Synology API response missing permanent_link"
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
    return await node_sync.upsert(observation)


async def resolve_name_conflict_and_upsert(
    *,
    request: web.Request,
    parent_id: MirrorStableId,
    name: str,
    media_info: MediaInfo | None,
) -> NodeRecord:
    """List Synology children and upsert the matching node (409 conflict recovery)."""
    node_sync = request.app[CHANGE_SERVICE_KEY]
    drive_api = request.app[SYNOLOGY_DRIVE_API_KEY]
    syno_paths = request.app[SYNOLOGY_PATH_KEY]

    info = await syno_paths.find_child_by_name(drive_api, parent_id, name)

    if info is None:
        raise web.HTTPConflict(
            reason=(
                f"Name conflict for {name!r} but existing node could not be resolved"
            )
        )

    return await enrich_and_upsert_synology_node(
        info=info,
        parent_id=parent_id,
        node_sync=node_sync,
        media_info=media_info,
    )

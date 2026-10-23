"""Shared utilities for handler modules."""

import functools
from collections.abc import Awaitable, Callable, Mapping

from aiohttp import web
from wcpan.drive.protocol.lib import NodeRecordDict, node_record_to_dict
from wcpan.drive.protocol.types import MediaInfo, NodeRecord

from ..keys import READY_KEY


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

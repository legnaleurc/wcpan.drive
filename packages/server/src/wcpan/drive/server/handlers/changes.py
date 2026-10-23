"""Change feed handlers: cursor, root, and incremental changes."""

from typing import Any

from aiohttp import web

from ..keys import STORAGE_KEY
from ..services.paths import SERVER_ROOT_ID
from .lib import record_to_response, require_ready


_MAX_CHANGES = 1000


@require_ready
async def get_cursor(request: web.Request) -> web.Response:
    storage = request.app[STORAGE_KEY]
    cursor = await storage.get_cursor()
    return web.json_response({"cursor": cursor})


@require_ready
async def get_root(request: web.Request) -> web.Response:
    storage = request.app[STORAGE_KEY]
    record = await storage.get_node_by_id(SERVER_ROOT_ID)
    if record is None:
        raise web.HTTPNotFound()
    return web.json_response(record_to_response(record))


@require_ready
async def get_changes(request: web.Request) -> web.Response:
    storage = request.app[STORAGE_KEY]
    try:
        cursor = int(request.rel_url.query.get("cursor", "0"))
        max_size = min(
            int(request.rel_url.query.get("max_size", str(_MAX_CHANGES))),
            _MAX_CHANGES,
        )
    except ValueError:
        raise web.HTTPBadRequest()

    rows, new_cursor, has_more = await storage.get_changes_since(cursor, max_size)

    changes: list[dict[str, Any]] = []
    for node_id, is_removed, record in rows:
        if is_removed:
            changes.append({"removed": True, "node_id": str(node_id)})
        elif record is not None:
            changes.append({"removed": False, "node": record_to_response(record)})

    return web.json_response(
        {
            "cursor": new_cursor,
            "has_more": has_more,
            "changes": changes,
        }
    )

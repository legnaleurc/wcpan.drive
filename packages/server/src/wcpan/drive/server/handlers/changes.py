"""Change feed handlers: cursor, root, and incremental changes."""

from typing import Any

from aiohttp import web

from ..keys import MIRROR_KEY
from .lib import record_to_response, require_ready


_MAX_CHANGES = 1000

# Starts from the beginning of whatever history the server has.
_BOOTSTRAP_CURSOR = "0"


def format_cursor(epoch: str, change_id: int) -> str:
    """A position in the change history named by *epoch*; opaque to clients."""
    return f"{epoch}.{change_id}"


def parse_cursor(cursor: str, *, epoch: str) -> int | None:
    """The change ID *cursor* points at, or None when it is stale.

    A cursor is stale when it comes from another history (a different epoch,
    or a legacy integer cursor). Raises ``ValueError`` when it is malformed.
    """
    if cursor == _BOOTSTRAP_CURSOR:
        return 0
    cursor_epoch, sep, raw_id = cursor.rpartition(".")
    if not sep:
        int(cursor)  # a legacy integer cursor is stale, anything else is bad
        return None
    if not cursor_epoch or not raw_id.isdigit():
        raise ValueError(cursor)
    if cursor_epoch != epoch:
        return None
    return int(raw_id)


def _stale() -> web.HTTPGone:
    return web.HTTPGone(
        text="cursor is stale; restart from the initial cursor",
        content_type="text/plain",
    )


@require_ready
async def get_cursor(request: web.Request) -> web.Response:
    mirror = request.app[MIRROR_KEY]
    change_id = await mirror.get_cursor()
    return web.json_response({"cursor": format_cursor(mirror.epoch, change_id)})


@require_ready
async def get_root(request: web.Request) -> web.Response:
    mirror = request.app[MIRROR_KEY]
    record = await mirror.get_root()
    if record is None:
        raise web.HTTPNotFound()
    return web.json_response(record_to_response(record))


@require_ready
async def get_changes(request: web.Request) -> web.Response:
    mirror = request.app[MIRROR_KEY]
    try:
        cursor = parse_cursor(
            request.rel_url.query.get("cursor", _BOOTSTRAP_CURSOR),
            epoch=mirror.epoch,
        )
        max_size = min(
            int(request.rel_url.query.get("max_size", str(_MAX_CHANGES))),
            _MAX_CHANGES,
        )
    except ValueError:
        raise web.HTTPBadRequest()
    # A position past the end means the database was restored from an older
    # copy under the same epoch; the client has seen changes that are gone.
    if cursor is None or cursor > await mirror.get_cursor():
        raise _stale()

    rows, new_cursor, has_more = await mirror.get_changes_since(cursor, max_size)

    changes: list[dict[str, Any]] = []
    for node_id, is_removed, record in rows:
        if is_removed:
            changes.append({"removed": True, "node_id": str(node_id)})
        elif record is not None:
            changes.append({"removed": False, "node": record_to_response(record)})

    return web.json_response(
        {
            "cursor": format_cursor(mirror.epoch, new_cursor),
            "has_more": has_more,
            "changes": changes,
        }
    )

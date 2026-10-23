"""Node CRUD, download, and single-shot upload handlers."""

from collections.abc import AsyncIterable
from contextlib import AbstractAsyncContextManager
from logging import getLogger

from aiohttp import web
from wcpan.drive.protocol.types import NodeId, NodeRecord

from .._keys import MIRROR_KEY, NODE_OPERATIONS_KEY, UPLOAD_SERVICE_KEY
from .._lib.names import normalize_name
from .._lib.ranges import RangeSourceError, serve_range
from .._services.mirror.store import (
    MirrorStore,
)
from .._services.operations import OperationConflictError, OperationUnavailableError
from .._services.upload.service import (
    UploadConflictError,
    UploadInvalidMediaError,
    UploadNameTooLongError,
    UploadParentGoneError,
    UploadPermanentError,
    UploadService,
    UploadTransientError,
)
from .lib import media_info_from_query, record_to_response, require_ready


_L = getLogger(__name__)


def _conflict_response(e: OperationConflictError) -> web.Response:
    if e.record is None:
        raise web.HTTPConflict(reason=str(e))
    return web.json_response(record_to_response(e.record), status=409)


@require_ready
async def get_node(request: web.Request) -> web.Response:
    mirror = request.app[MIRROR_KEY]
    node_id = NodeId(request.match_info["id"])
    record = await mirror.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()
    return web.json_response(record_to_response(record))


@require_ready
async def download_node(request: web.Request) -> web.StreamResponse:
    mirror = request.app[MIRROR_KEY]
    node_id = NodeId(request.match_info["id"])
    record = await mirror.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

    operations = request.app[NODE_OPERATIONS_KEY]

    def open_range(
        offset: int, length: int
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        return operations.download(record, offset=offset, length=length)

    try:
        # Ranges resolve against the mirrored size; see the Publication rule.
        return await serve_range(
            request,
            size=record.size,
            content_type=record.mime_type or "application/octet-stream",
            open_range=open_range,
        )
    except OperationUnavailableError as e:
        raise web.HTTPServiceUnavailable(reason=str(e)) from e
    except RangeSourceError as e:
        # The status line is already sent; letting the error escape aborts the
        # connection.
        _L.warning("Download stream error for node %s: %s", node_id, e)
        raise


@require_ready
async def create_node(request: web.Request) -> web.Response:
    """Create a directory."""
    operations = request.app[NODE_OPERATIONS_KEY]

    body = await request.json()
    name: str = body.get("name", "")
    parent_id_raw = body.get("parent_id", "")

    if not name or not parent_id_raw:
        raise web.HTTPBadRequest()
    name = normalize_name(name)
    parent_id = NodeId(parent_id_raw)

    try:
        record = await operations.create_folder(parent_id, name)
    except OperationConflictError as e:
        return _conflict_response(e)
    except OperationUnavailableError as e:
        raise web.HTTPServiceUnavailable(reason=str(e))
    return web.json_response(record_to_response(record), status=201)


async def _is_structural(record: NodeRecord, *, mirror: MirrorStore) -> bool:
    """Whether *record* is the server root or a mount node.

    The root is the only node without a parent, and mounts are its only
    children, so both are recognized by position in the tree.
    """
    if record.parent_id is None:
        return True
    parent = await mirror.get_node_by_id(record.parent_id)
    return parent is not None and parent.parent_id is None


@require_ready
async def update_node(request: web.Request) -> web.Response:
    """Rename and/or move a node."""
    mirror = request.app[MIRROR_KEY]
    operations = request.app[NODE_OPERATIONS_KEY]
    node_id = NodeId(request.match_info["id"])

    record = await mirror.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()
    if await _is_structural(record, mirror=mirror):
        raise web.HTTPForbidden(reason="Cannot modify virtual nodes")

    body = await request.json()
    new_name: str | None = body.get("name")
    if new_name:
        new_name = normalize_name(new_name)
    new_parent_id = (
        NodeId(body["parent_id"]) if body.get("parent_id") is not None else None
    )

    try:
        updated = await operations.update(
            record, name=new_name, parent_id=new_parent_id
        )
    except OperationConflictError as e:
        return _conflict_response(e)
    except OperationUnavailableError as e:
        raise web.HTTPServiceUnavailable(reason=str(e))
    return web.json_response(record_to_response(updated))


@require_ready
async def delete_node(request: web.Request) -> web.Response:
    mirror = request.app[MIRROR_KEY]
    operations = request.app[NODE_OPERATIONS_KEY]
    node_id = NodeId(request.match_info["id"])

    record = await mirror.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()
    if await _is_structural(record, mirror=mirror):
        raise web.HTTPForbidden(reason="Cannot delete virtual nodes")

    try:
        await operations.delete(record)
    except OperationUnavailableError as e:
        raise web.HTTPServiceUnavailable(reason=str(e))
    return web.Response(status=204)


@require_ready
async def upload_node(request: web.Request) -> web.Response:
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    parent_id = NodeId(request.match_info["parent_id"])

    name = request.rel_url.query.get("name", "")
    if not name:
        raise web.HTTPBadRequest(reason="Missing 'name' query parameter")
    name = normalize_name(name)

    mime_type = request.rel_url.query.get("mime_type") or None
    media_info = media_info_from_query(request.rel_url.query)
    _L.debug(
        "upload name=%r parent_id=%r mime_type=%r media_info=%r",
        name,
        parent_id,
        mime_type,
        media_info,
    )

    try:
        result = await upload_service.upload_direct(
            parent_id=parent_id,
            name=name,
            content=request.content.iter_any(),
            mime_type=mime_type,
            media_info=media_info,
        )
    except UploadConflictError as e:
        return web.json_response(record_to_response(e.record), status=409)
    except UploadTransientError as e:
        raise web.HTTPServiceUnavailable(reason=str(e), headers={"Retry-After": "5"})
    except UploadNameTooLongError as e:
        return web.json_response(
            {"error": "name_too_long", "message": str(e), "name": e.name},
            status=422,
        )
    except UploadInvalidMediaError as e:
        return web.json_response(
            {"error": "invalid_media", "message": str(e)}, status=422
        )
    except UploadParentGoneError as e:
        raise web.HTTPGone(reason=str(e))
    except UploadPermanentError as e:
        raise web.HTTPInsufficientStorage(reason=str(e))

    return web.json_response(record_to_response(result.record), status=201)

"""Node CRUD, download, and single-shot upload handlers."""

from logging import getLogger

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from wcpan.drive.protocol.types import NodeId

from ..keys import NODE_OPERATIONS_KEY, STORAGE_KEY, UPLOAD_SERVICE_KEY
from ..lib.names import normalize_name
from ..services.operations import OperationConflictError, OperationUnavailableError
from ..services.paths import SERVER_ROOT_ID, is_virtual
from ..services.upload import (
    UploadConflictError,
    UploadNameTooLongError,
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
    storage = request.app[STORAGE_KEY]
    node_id = NodeId(request.match_info["id"])
    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()
    return web.json_response(record_to_response(record))


@require_ready
async def download_node(request: web.Request) -> web.StreamResponse:
    storage = request.app[STORAGE_KEY]
    node_id = NodeId(request.match_info["id"])
    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

    operations = request.app[NODE_OPERATIONS_KEY]

    range_ = request.http_range if "Range" in request.headers else None

    response = web.StreamResponse(
        status=206 if range_ else 200,
        headers={"Content-Type": record.mime_type or "application/octet-stream"},
    )
    await response.prepare(request)

    try:
        async with operations.download(record, range_=range_) as chunks:
            async for chunk in chunks:
                await response.write(chunk)
        await response.write_eof()
    except (ConnectionError, ClientConnectionResetError):
        pass  # client disconnected
    except OperationUnavailableError as e:
        _L.warning("Download stream error for node %s: %s", node_id, e)

    return response


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


@require_ready
async def update_node(request: web.Request) -> web.Response:
    """Rename and/or move a node."""
    storage = request.app[STORAGE_KEY]
    operations = request.app[NODE_OPERATIONS_KEY]
    node_id = NodeId(request.match_info["id"])

    if is_virtual(node_id):
        raise web.HTTPForbidden(reason="Cannot modify virtual nodes")

    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

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
    storage = request.app[STORAGE_KEY]
    operations = request.app[NODE_OPERATIONS_KEY]
    node_id = NodeId(request.match_info["id"])

    if is_virtual(node_id):
        raise web.HTTPForbidden(reason="Cannot delete virtual nodes")

    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

    await operations.delete(record)
    return web.Response(status=204)


@require_ready
async def upload_node(request: web.Request) -> web.Response:
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    parent_id = NodeId(request.match_info["parent_id"])

    if parent_id == SERVER_ROOT_ID:
        raise web.HTTPForbidden(reason="Cannot upload to virtual root")

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
    except UploadPermanentError as e:
        raise web.HTTPInsufficientStorage(reason=str(e))

    return web.json_response(record_to_response(result.record), status=201)

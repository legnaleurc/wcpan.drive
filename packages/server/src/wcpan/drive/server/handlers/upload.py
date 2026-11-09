"""Resumable upload session handlers."""

from aiohttp import web
from wcpan.drive.protocol.types import MediaInfo, NodeId

from ..keys import READY_KEY, UPLOAD_SERVICE_KEY
from ..services.paths import SERVER_ROOT_ID
from ..services.upload import (
    UploadAccepted,
    UploadBusyError,
    UploadConflictError,
    UploadCreatedNode,
    UploadInvalidChunkError,
    UploadNameTooLongError,
    UploadOffsetMismatchError,
    UploadParentGoneError,
    UploadPermanentError,
    UploadService,
    UploadSessionNotFoundError,
    UploadTransientError,
)
from .lib import record_to_response, require_ready


_MEDIA_FIELDS = ("width", "height", "ms_duration", "media_image", "media_video")


def _media_info_from_body(body: dict[str, object]) -> MediaInfo | None:
    if not any(k in body for k in _MEDIA_FIELDS):
        return None
    return MediaInfo(
        width=int(str(body.get("width", 0))),
        height=int(str(body.get("height", 0))),
        ms_duration=int(str(body.get("ms_duration", 0))),
        is_image=bool(body.get("media_image", False)),
        is_video=bool(body.get("media_video", False)),
    )


@require_ready
async def create_upload_session(request: web.Request) -> web.Response:
    """POST /api/v1/nodes/{parent_id}/uploads"""
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    parent_id = NodeId(request.match_info["parent_id"])

    if parent_id == SERVER_ROOT_ID:
        raise web.HTTPForbidden(reason="Cannot upload to virtual root")

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    name = body.get("name", "")
    if not name:
        raise web.HTTPBadRequest(reason="Missing 'name' in request body")

    raw_size = body.get("size")
    if raw_size is None:
        raise web.HTTPBadRequest(reason="Missing 'size' in request body")
    try:
        total_size = int(raw_size)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason="Invalid 'size': must be an integer")
    if total_size <= 0:
        raise web.HTTPBadRequest(reason="'size' must be a positive integer")

    mime_type = body.get("mime_type") or None
    media_info = _media_info_from_body(body)
    try:
        created = await upload_service.create_session(
            parent_id=parent_id,
            name=name,
            total_size=total_size,
            mime_type=mime_type,
            media_info=media_info,
        )
    except UploadConflictError as e:
        return web.json_response(record_to_response(e.record), status=409)

    return web.Response(
        status=201,
        headers={
            "Location": f"/api/v1/uploads/{created.session_id}",
            "Upload-Length": str(created.total_size),
        },
    )


async def patch_upload_chunk(request: web.Request) -> web.Response:
    """PATCH /api/v1/uploads/{session_id}"""
    if not request.app[READY_KEY]:
        raise web.HTTPServiceUnavailable(reason="Server not ready")

    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    session_id = request.match_info["session_id"]

    offset_str = request.headers.get("Upload-Offset")
    if offset_str is None:
        raise web.HTTPBadRequest(reason="Missing Upload-Offset header")
    try:
        start = int(offset_str)
    except ValueError:
        raise web.HTTPBadRequest(reason="Invalid Upload-Offset: must be an integer")
    if start < 0:
        raise web.HTTPBadRequest(reason="Invalid Upload-Offset: must be non-negative")

    try:
        result = await upload_service.append_chunk(
            session_id=session_id,
            start=start,
            content=request.content.iter_chunked(4 * 1024 * 1024),
        )
    except UploadSessionNotFoundError:
        raise web.HTTPNotFound()
    except UploadInvalidChunkError as e:
        raise web.HTTPBadRequest(reason=str(e))
    except UploadOffsetMismatchError as e:
        return web.Response(status=409, headers={"Upload-Offset": str(e.offset)})
    except UploadConflictError as e:
        return web.json_response(record_to_response(e.record), status=409)
    except UploadTransientError as e:
        raise web.HTTPServiceUnavailable(reason=str(e), headers={"Retry-After": "5"})
    except UploadNameTooLongError as e:
        return web.json_response(
            {"error": "name_too_long", "message": str(e), "name": e.name},
            status=422,
        )
    except UploadParentGoneError as e:
        raise web.HTTPGone(reason=str(e))
    except UploadPermanentError as e:
        raise web.HTTPInsufficientStorage(reason=str(e))

    match result:
        case UploadAccepted(offset=offset):
            return web.Response(status=204, headers={"Upload-Offset": str(offset)})
        case UploadCreatedNode(record=record):
            return web.json_response(record_to_response(record), status=201)
        case _:
            raise RuntimeError(f"unexpected append result: {type(result)!r}")


async def head_upload_session(request: web.Request) -> web.Response:
    """HEAD /api/v1/uploads/{session_id}"""
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    session_id = request.match_info["session_id"]
    status = upload_service.get_session_status(session_id)
    if status is None:
        raise web.HTTPNotFound()
    return web.Response(
        status=200,
        headers={
            "Upload-Offset": str(status.received),
            "Upload-Length": str(status.total_size),
        },
    )


async def delete_upload_session(request: web.Request) -> web.Response:
    """DELETE /api/v1/uploads/{session_id}"""
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    session_id = request.match_info["session_id"]
    try:
        deleted = await upload_service.delete_session(session_id)
    except UploadBusyError:
        # Publishing has started; the client finds the result by looking up
        # the node instead.
        raise web.HTTPConflict(reason="Upload is being published")
    if not deleted:
        raise web.HTTPNotFound()
    return web.Response(status=204)

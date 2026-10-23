"""Node CRUD, download, and single-shot upload handlers."""

from logging import getLogger

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from wcpan.drive.protocol.lib import guess_mime_type, utc_now
from wcpan.drive.protocol.types import MirrorMutableId, MirrorStableId, NodeRecord
from wcpan.drive.server.synology import convert_file_info
from wcpan.synology import (
    SynologyClient,
    SynologyNetworkError,
    SynologyPermanentLink,
    SynologyUploadConflictError,
)

from ..keys import (
    CHANGE_SERVICE_KEY,
    STORAGE_KEY,
    SYNOLOGY_DRIVE_API_KEY,
    SYNOLOGY_PATH_KEY,
    UPLOAD_SERVICE_KEY,
)
from ..lib.names import normalize_name
from ..services.paths import SERVER_ROOT_ID, SynologyPathService, is_virtual
from ..services.upload import (
    UploadConflictError,
    UploadNameTooLongError,
    UploadPermanentError,
    UploadService,
    UploadTransientError,
)
from .lib import (
    media_info_from_query,
    record_to_response,
    require_ready,
    resolve_name_conflict_and_upsert,
)


_L = getLogger(__name__)


def _stable_node_ref(record: NodeRecord) -> SynologyPermanentLink:
    return SynologyPermanentLink(str(record.id))


async def _resolve_actual_record_after_move(
    drive_api: SynologyClient,
    syno_paths: SynologyPathService,
    *,
    moved_node_ref: SynologyPermanentLink,
    expected_parent_node_id: MirrorStableId,
    expected_name: str,
) -> NodeRecord | None:
    info = await drive_api.get_file(moved_node_ref)
    if info is None:
        info = await syno_paths.find_child_by_name(
            drive_api,
            expected_parent_node_id,
            expected_name,
        )
    if info is None:
        return None
    return convert_file_info(info, expected_parent_node_id)


@require_ready
async def get_node(request: web.Request) -> web.Response:
    storage = request.app[STORAGE_KEY]
    node_id = MirrorStableId(request.match_info["id"])
    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()
    return web.json_response(record_to_response(record))


@require_ready
async def download_node(request: web.Request) -> web.StreamResponse:
    storage = request.app[STORAGE_KEY]
    node_id = MirrorStableId(request.match_info["id"])
    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

    drive_api = request.app[SYNOLOGY_DRIVE_API_KEY]

    range_ = request.http_range if "Range" in request.headers else None

    response = web.StreamResponse(
        status=206 if range_ else 200,
        headers={"Content-Type": record.mime_type or "application/octet-stream"},
    )
    await response.prepare(request)

    try:
        async with drive_api.download(
            _stable_node_ref(record), range_=range_
        ) as syno_response:
            async for chunk in syno_response.content.iter_any():
                await response.write(chunk)
        await response.write_eof()
    except (ConnectionError, ClientConnectionResetError):
        pass  # client disconnected
    except SynologyNetworkError as e:
        if isinstance(e.original_error, (ConnectionError, ClientConnectionResetError)):
            pass  # client disconnect wrapped by drive_api.download
        else:
            _L.warning("Download stream error for node %s: %s", node_id, e)

    return response


@require_ready
async def create_node(request: web.Request) -> web.Response:
    """Create a directory."""
    node_sync = request.app[CHANGE_SERVICE_KEY]
    drive_api = request.app[SYNOLOGY_DRIVE_API_KEY]
    syno_paths = request.app[SYNOLOGY_PATH_KEY]

    body = await request.json()
    name: str = body.get("name", "")
    parent_id_raw = body.get("parent_id", "")

    if not name or not parent_id_raw:
        raise web.HTTPBadRequest()
    name = normalize_name(name)
    parent_id = MirrorStableId(parent_id_raw)

    parent_ref = await syno_paths.synology_parent_ref(parent_id)
    try:
        info = await drive_api.create_folder(parent_ref, name)
    except SynologyUploadConflictError:
        record = await resolve_name_conflict_and_upsert(
            request=request,
            parent_id=parent_id,
            name=name,
            media_info=None,
        )
        return web.json_response(record_to_response(record), status=409)

    permanent_link = info.get("permanent_link")
    if not permanent_link:
        _L.warning(
            "create_folder response missing permanent_link for file_id=%s; aborting",
            info["file_id"],
        )
        raise web.HTTPServiceUnavailable(
            reason="Synology did not return permanent_link for new folder"
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
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        mutable_id=MirrorMutableId(info["file_id"]),
    )
    record = await node_sync.upsert(record)
    return web.json_response(record_to_response(record), status=201)


@require_ready
async def update_node(request: web.Request) -> web.Response:
    """Rename and/or move a node."""
    storage = request.app[STORAGE_KEY]
    node_sync = request.app[CHANGE_SERVICE_KEY]
    drive_api = request.app[SYNOLOGY_DRIVE_API_KEY]
    syno_paths = request.app[SYNOLOGY_PATH_KEY]
    node_id = MirrorStableId(request.match_info["id"])

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
        MirrorStableId(body["parent_id"]) if body.get("parent_id") is not None else None
    )

    updated_record = record

    if new_name and new_name != record.name:
        try:
            info = await drive_api.rename(
                _stable_node_ref(updated_record),
                new_name,
            )
        except SynologyUploadConflictError:
            raise web.HTTPConflict(reason=f"{new_name!r} already exists in this folder")
        updated_record = NodeRecord(
            id=updated_record.id,
            parent_id=updated_record.parent_id,
            name=info["name"],
            is_directory=updated_record.is_directory,
            created_time=info.get("created_time", 0) or updated_record.created_time,
            modified_time=info.get("modified_time", 0) or updated_record.modified_time,
            changed_time=info.get("change_time", 0) or updated_record.changed_time,
            mime_type=guess_mime_type(
                info["name"], is_directory=updated_record.is_directory
            ),
            hash=info.get("hash", updated_record.hash),
            size=info.get("size", updated_record.size),
            is_image=updated_record.is_image,
            is_video=updated_record.is_video,
            width=updated_record.width,
            height=updated_record.height,
            ms_duration=updated_record.ms_duration,
            mutable_id=MirrorMutableId(raw)
            if (raw := info.get("file_id"))
            else updated_record.mutable_id,
        )

    if new_parent_id and new_parent_id != record.parent_id:
        new_parent_ref = await syno_paths.synology_parent_ref(new_parent_id)
        try:
            await drive_api.move(
                _stable_node_ref(updated_record),
                new_parent_ref,
            )
        except Exception as e:
            raise web.HTTPServiceUnavailable(reason=str(e))
        actual_record = await _resolve_actual_record_after_move(
            drive_api,
            syno_paths,
            moved_node_ref=_stable_node_ref(updated_record),
            expected_parent_node_id=new_parent_id,
            expected_name=updated_record.name,
        )
        if actual_record is None:
            updated_record = NodeRecord(
                id=updated_record.id,
                parent_id=new_parent_id,
                name=updated_record.name,
                is_directory=updated_record.is_directory,
                created_time=updated_record.created_time,
                modified_time=updated_record.modified_time,
                changed_time=utc_now(),
                mime_type=updated_record.mime_type,
                hash=updated_record.hash,
                size=updated_record.size,
                is_image=updated_record.is_image,
                is_video=updated_record.is_video,
                width=updated_record.width,
                height=updated_record.height,
                ms_duration=updated_record.ms_duration,
                mutable_id=updated_record.mutable_id,
            )
        else:
            updated_record = actual_record
            if actual_record.id != record.id:
                await node_sync.delete(record.id)

    updated_record = await node_sync.upsert(updated_record)
    return web.json_response(record_to_response(updated_record))


@require_ready
async def delete_node(request: web.Request) -> web.Response:
    storage = request.app[STORAGE_KEY]
    node_sync = request.app[CHANGE_SERVICE_KEY]
    drive_api = request.app[SYNOLOGY_DRIVE_API_KEY]
    node_id = MirrorStableId(request.match_info["id"])

    if is_virtual(node_id):
        raise web.HTTPForbidden(reason="Cannot delete virtual nodes")

    record = await storage.get_node_by_id(node_id)
    if record is None:
        raise web.HTTPNotFound()

    await drive_api.delete(_stable_node_ref(record))
    await node_sync.delete(node_id)
    return web.Response(status=204)


@require_ready
async def upload_node(request: web.Request) -> web.Response:
    upload_service: UploadService = request.app[UPLOAD_SERVICE_KEY]
    parent_id = MirrorStableId(request.match_info["parent_id"])

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

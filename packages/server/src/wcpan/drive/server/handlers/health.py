"""Shallow health probe and diagnostic handlers."""

from logging import getLogger
from time import monotonic

from aiohttp import web

from ..keys import READY_KEY


_L = getLogger(__name__)


async def get_livez(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def get_readyz(request: web.Request) -> web.Response:
    is_ready = bool(request.app.get(READY_KEY, False))
    if not is_ready:
        raise web.HTTPServiceUnavailable(
            text='{"ok": false, "ready": false}',
            content_type="application/json",
        )
    return web.json_response({"ok": True, "ready": True})


async def put_null(request: web.Request) -> web.Response:
    started_at = monotonic()
    bytes_received = 0
    async for chunk in request.content.iter_any():
        bytes_received += len(chunk)
    elapsed_seconds = monotonic() - started_at
    bytes_per_second = bytes_received / elapsed_seconds if elapsed_seconds > 0 else 0.0
    mebibytes_per_second = bytes_per_second / (1024 * 1024)
    _L.debug(
        "upload sink consumed %d byte(s) in %.6f s",
        bytes_received,
        elapsed_seconds,
    )
    return web.json_response(
        {
            "bytes_received": bytes_received,
            "elapsed_seconds": elapsed_seconds,
            "bytes_per_second": bytes_per_second,
            "mebibytes_per_second": mebibytes_per_second,
        }
    )

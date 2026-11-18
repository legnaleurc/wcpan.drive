"""Synology webhook HTTP handler."""

from aiohttp import web
from aiohttp.typedefs import Handler
from wcpan.drive.server.types import HintStage
from wcpan.synology.types import SynologyWebhookEvent


def webhook_handler(hints: HintStage) -> Handler:
    """Queue webhook events for one backend instance.

    Synology Drive may already batch nearby events before delivering them.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        data: list[SynologyWebhookEvent] = await request.json()
        for item in data:
            hints.publish(item)
        return web.Response(text="OK")

    return handle

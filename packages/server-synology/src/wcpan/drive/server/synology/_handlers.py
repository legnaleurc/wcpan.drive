"""Synology webhook HTTP handler."""

from aiohttp import web
from aiohttp.typedefs import Handler
from wcpan.synology import SynologyWebhookEvent

from ._webhook import WebhookQueue


def webhook_handler(queue: WebhookQueue) -> Handler:
    """Queue webhook events for one backend instance.

    Synology Drive may already batch nearby events before delivering them.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        data: list[SynologyWebhookEvent] = await request.json()
        for item in data:
            await queue.put(item)
        return web.Response(text="OK")

    return handle

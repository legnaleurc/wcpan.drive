"""Synology webhook HTTP handler."""

from aiohttp import web
from wcpan.synology import SynologyWebhookEvent

from ..keys import WEBHOOK_QUEUE_KEY


# Synology Drive may already batch nearby events before delivering the webhook.
async def handle_synology_webhook(request: web.Request) -> web.Response:
    data: list[SynologyWebhookEvent] = await request.json()
    webhook_queue = request.app[WEBHOOK_QUEUE_KEY]
    for item in data:
        await webhook_queue.put(item)
    return web.Response(text="OK")

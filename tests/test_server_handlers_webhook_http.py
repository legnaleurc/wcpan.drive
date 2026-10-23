"""Tests for webhook HTTP handler logging and queueing."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from wcpan.drive.server.synology.handlers import handle_synology_webhook
from wcpan.drive.server.synology.keys import WEBHOOK_QUEUE_KEY


class TestWebhookHttpHandler(IsolatedAsyncioTestCase):
    async def test_logs_and_enqueues_items(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        payload = [
            {"event_type": "file_created", "file_id": "f1"},
            {"event_type": "file_removed", "file_id": "f2"},
        ]
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value=payload)
        request.app = {WEBHOOK_QUEUE_KEY: queue}

        resp = await handle_synology_webhook(request)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(await queue.get(), payload[0])
        self.assertEqual(await queue.get(), payload[1])

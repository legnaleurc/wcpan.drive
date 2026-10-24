"""Tests for webhook HTTP handler logging and queueing."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from wcpan.drive.server.synology.handlers import webhook_handler


class TestWebhookHttpHandler(IsolatedAsyncioTestCase):
    async def test_logs_and_enqueues_items(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        payload = [
            {"event_type": "file_created", "file_id": "f1"},
            {"event_type": "file_removed", "file_id": "f2"},
        ]
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value=payload)

        resp = await webhook_handler(queue)(request)
        assert isinstance(resp, web.Response)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(await queue.get(), payload[0])
        self.assertEqual(await queue.get(), payload[1])

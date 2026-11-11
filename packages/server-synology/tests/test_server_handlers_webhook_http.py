"""Tests for webhook HTTP handler logging and queueing."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from wcpan.drive.server.synology._handlers import webhook_handler

from ._lib import make_request


class TestWebhookHttpHandler(IsolatedAsyncioTestCase):
    async def test_logs_and_enqueues_items(self) -> None:
        # given
        queue: asyncio.Queue = asyncio.Queue()
        payload = [
            {"event_type": "file_created", "file_id": "f1"},
            {"event_type": "file_removed", "file_id": "f2"},
        ]
        request = make_request(json_body=payload)

        # when
        resp = await webhook_handler(queue)(request)
        # then
        assert isinstance(resp, web.Response)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(await queue.get(), payload[0])
        self.assertEqual(await queue.get(), payload[1])

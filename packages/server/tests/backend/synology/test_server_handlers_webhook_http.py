"""Tests for webhook HTTP handler logging and queueing."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from wcpan.drive.server.backend.synology._handlers import webhook_handler
from wcpan.drive.server.types import ChangeHandler, HintStage

from ._lib import make_request


class _Collect(ChangeHandler[object]):
    def __init__(self) -> None:
        self.events: list[object] = []

    async def handle(self, event: object) -> None:
        self.events.append(event)


class TestWebhookHttpHandler(IsolatedAsyncioTestCase):
    async def test_logs_and_enqueues_items(self) -> None:
        # given
        hints = HintStage()
        payload = [
            {"event_type": "file_created", "file_id": "f1"},
            {"event_type": "file_removed", "file_id": "f2"},
        ]
        request = make_request(json_body=payload)

        # when
        resp = await webhook_handler(hints)(request)
        # then
        assert isinstance(resp, web.Response)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")
        handler = _Collect()
        consumer = asyncio.create_task(hints.run(handler))
        try:
            async with asyncio.timeout(5):
                await hints.join()
        finally:
            consumer.cancel()
        self.assertEqual(handler.events, payload)

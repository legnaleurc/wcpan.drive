"""Client parsing of 409 responses with a node record body."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from wcpan.drive.client.feed.http409 import node_from_409


class TestNodeFrom409(IsolatedAsyncioTestCase):
    async def test_returns_node_when_body_has_id(self) -> None:
        now = 1_704_067_200
        payload = {
            "id": "n1",
            "parent_id": "p",
            "name": "x",
            "is_directory": True,
            "created_time": now,
            "modified_time": now,
            "changed_time": now,
            "mime_type": "application/x-directory",
            "hash": "",
            "hash_algorithm": "",
            "size": 0,
            "is_image": False,
            "is_video": False,
            "width": 0,
            "height": 0,
            "ms_duration": 0,
        }
        resp = MagicMock()
        resp.json = AsyncMock(return_value=payload)
        node = await node_from_409(resp)
        assert node is not None
        self.assertEqual(node.id, "n1")
        self.assertEqual(node.parent_id, "p")

    async def test_returns_none_for_received_only_body(self) -> None:
        resp = MagicMock()
        resp.json = AsyncMock(return_value={"received": 50})
        node = await node_from_409(resp)
        self.assertIsNone(node)

    async def test_returns_none_on_invalid_json(self) -> None:
        resp = MagicMock()
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        node = await node_from_409(resp)
        self.assertIsNone(node)

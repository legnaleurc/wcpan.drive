"""Client parsing of 409 responses with a node record body."""

from wcpan.drive.client.feed.http409 import node_from_409

from ._lib import DecoyTestCase


class TestNodeFrom409(DecoyTestCase):
    async def test_returns_node_when_body_has_id(self) -> None:
        # given
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
        resp = await self.create_response(409, body=payload)
        # when
        node = await node_from_409(resp)
        # then
        assert node is not None
        self.assertEqual(node.id, "n1")
        self.assertEqual(node.parent_id, "p")

    async def test_returns_none_for_received_only_body(self) -> None:
        # given
        resp = await self.create_response(409, body={"received": 50})
        # when
        node = await node_from_409(resp)
        # then
        self.assertIsNone(node)

    async def test_returns_none_on_invalid_json(self) -> None:
        # given
        resp = await self.create_response(409)
        self.decoy.when(await resp.json()).then_raise(ValueError("bad json"))
        # when
        node = await node_from_409(resp)
        # then
        self.assertIsNone(node)

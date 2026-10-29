from pathlib import PurePath
from unittest.mock import patch

from decoy import matchers
from wcpan.drive.client.cli._cmd.lib import (
    get_node_by_id_or_path,
    get_path_by_id_or_path,
    require_authenticated,
)
from wcpan.drive.client.exceptions import AuthenticationError
from wcpan.drive.client.types import Drive

from ._lib import DecoyTestCase, make_node


class TestGetNodeByIdOrPath(DecoyTestCase):
    async def test_slash_prefix_calls_get_node_by_path(self):
        drive = self.decoy.mock(cls=Drive)
        node = make_node()
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/some/path"))
        ).then_return(node)
        result = await get_node_by_id_or_path(drive, "/some/path")
        self.assertIs(result, node)
        self.decoy.verify(await drive.get_node_by_id(matchers.Anything()), times=0)

    async def test_no_slash_calls_get_node_by_id(self):
        drive = self.decoy.mock(cls=Drive)
        node = make_node()
        self.decoy.when(await drive.get_node_by_id("node-id-123")).then_return(node)
        result = await get_node_by_id_or_path(drive, "node-id-123")
        self.assertIs(result, node)
        self.decoy.verify(await drive.get_node_by_path(matchers.Anything()), times=0)


class TestGetPathByIdOrPath(DecoyTestCase):
    async def test_slash_prefix_returns_purepath_directly(self):
        drive = self.decoy.mock(cls=Drive)
        result = await get_path_by_id_or_path(drive, "/some/path")
        self.assertEqual(result, PurePath("/some/path"))
        self.decoy.verify(await drive.get_node_by_id(matchers.Anything()), times=0)
        self.decoy.verify(await drive.resolve_path(matchers.Anything()), times=0)

    async def test_no_slash_calls_get_node_by_id_and_resolve_path(self):
        drive = self.decoy.mock(cls=Drive)
        node = make_node(id="node-id-123")
        expected_path = PurePath("/resolved/path")
        self.decoy.when(await drive.get_node_by_id("node-id-123")).then_return(node)
        self.decoy.when(await drive.resolve_path(node)).then_return(expected_path)
        result = await get_path_by_id_or_path(drive, "node-id-123")
        self.assertEqual(result, expected_path)


class TestRequireAuthenticated(DecoyTestCase):
    async def test_success_returns_wrapped_result(self):
        async def fn() -> int:
            return 0

        wrapped = require_authenticated(fn)
        result = await wrapped()
        self.assertEqual(result, 0)

    async def test_authentication_error_returns_1(self):
        async def fn() -> int:
            raise AuthenticationError

        wrapped = require_authenticated(fn)
        with patch("wcpan.drive.client.cli._cmd.lib.cout") as mock_cout:
            result = await wrapped()
        self.assertEqual(result, 1)
        mock_cout.assert_called_once_with("not authenticated")

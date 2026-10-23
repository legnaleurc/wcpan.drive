from pathlib import PurePath
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from wcpan.drive.client.cli._cmd.lib import (
    get_node_by_id_or_path,
    get_path_by_id_or_path,
    require_authenticated,
)
from wcpan.drive.client.exceptions import AuthenticationError
from wcpan.drive.client.types import Drive

from ._lib import aexpect, create_amock


class TestGetNodeByIdOrPath(IsolatedAsyncioTestCase):
    async def test_slash_prefix_calls_get_node_by_path(self):
        drive = create_amock(Drive)
        await get_node_by_id_or_path(drive, "/some/path")
        aexpect(drive.get_node_by_path).assert_called_once_with(PurePath("/some/path"))
        aexpect(drive.get_node_by_id).assert_not_called()

    async def test_no_slash_calls_get_node_by_id(self):
        drive = create_amock(Drive)
        await get_node_by_id_or_path(drive, "node-id-123")
        aexpect(drive.get_node_by_id).assert_called_once_with("node-id-123")
        aexpect(drive.get_node_by_path).assert_not_called()


class TestGetPathByIdOrPath(IsolatedAsyncioTestCase):
    async def test_slash_prefix_returns_purepath_directly(self):
        drive = create_amock(Drive)
        result = await get_path_by_id_or_path(drive, "/some/path")
        self.assertEqual(result, PurePath("/some/path"))
        aexpect(drive.get_node_by_id).assert_not_called()
        aexpect(drive.resolve_path).assert_not_called()

    async def test_no_slash_calls_get_node_by_id_and_resolve_path(self):
        drive = create_amock(Drive)
        expected_path = PurePath("/resolved/path")
        aexpect(drive.resolve_path).return_value = expected_path
        result = await get_path_by_id_or_path(drive, "node-id-123")
        aexpect(drive.get_node_by_id).assert_called_once_with("node-id-123")
        aexpect(drive.resolve_path).assert_called_once()
        self.assertEqual(result, expected_path)


class TestRequireAuthenticated(IsolatedAsyncioTestCase):
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

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.synology import create_synology_client
from wcpan.drive.server.types import ServerConfig
from wcpan.synology import SynologyPath


def _config() -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=8080,
        database_url="sqlite:///tmp/test.sqlite",
        synology_url="https://nas.example",
        username="user",
        password="secret",
        mounts={"docs": SynologyPath("/docs")},
        public_url="https://public.example",
        webhook_app_id="app-id",
        local_paths={},
    )


def _cm(value: object) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestCreateSynologyClient(IsolatedAsyncioTestCase):
    async def test_create_webstation_api(self) -> None:
        client = MagicMock()
        with patch(
            "wcpan.drive.server.synology.create_client",
            return_value=_cm(client),
        ) as create:
            async with create_synology_client(_config()) as api:
                self.assertIs(api, client)

        create.assert_called_once_with(
            base_url="https://nas.example",
            username="user",
            password="secret",
            otp_code=None,
        )

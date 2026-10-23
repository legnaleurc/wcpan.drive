from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.synology.config import SynologyConfig, create_synology_client


def _config() -> SynologyConfig:
    return SynologyConfig(
        url="https://nas.example",
        username="user",
        password="secret",
        public_url="https://public.example",
        webhook_app_id="app-id",
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
            "wcpan.drive.server.synology.config.create_client",
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

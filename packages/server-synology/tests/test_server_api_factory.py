from unittest.mock import patch

from wcpan.drive.server.synology.config import SynologyConfig, create_synology_client
from wcpan.synology import SynologyClient, create_client

from ._lib import DecoyTestCase


def _config() -> SynologyConfig:
    return SynologyConfig(
        url="https://nas.example",
        username="user",
        password="secret",
        public_url="https://public.example",
        webhook_app_id="app-id",
    )


class TestCreateSynologyClient(DecoyTestCase):
    async def test_create_webstation_api(self) -> None:
        client = self.decoy.mock(cls=SynologyClient)
        create = self.decoy.mock(func=create_client)
        self.decoy.when(
            create(
                base_url="https://nas.example",
                username="user",
                password="secret",
                otp_code=None,
            )
        ).then_enter_with(client)
        with patch("wcpan.drive.server.synology.config.create_client", create):
            async with create_synology_client(_config()) as api:
                self.assertIs(api, client)

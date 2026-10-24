"""Synology backend settings and instances."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.backend import BackendContext, BackendRoutes, CoreServices
from wcpan.drive.server.synology import create_backend
from wcpan.drive.server.synology.config import parse_synology_config


def _settings(**overrides):
    settings = {
        "url": "https://nas.example",
        "username": "user",
        "password": "secret",
        "webhook": {"callback_url": "https://public.example"},
    }
    settings.update(overrides)
    return settings


class TestSettings(unittest.TestCase):
    def test_defaults(self):
        config = parse_synology_config(_settings())
        self.assertEqual(config.webhook_app_id, "wcpan-drive-server-synology")
        self.assertIsNone(config.otp_code)
        self.assertEqual(config.local_paths, {})

    def test_maps_nested_settings(self):
        config = parse_synology_config(
            _settings(
                url="https://nas.example:5001",
                otp_code="123456",
                webhook={"callback_url": "https://callback.example", "app_id": "app-2"},
                local_paths={"/volume1": "/mnt/nas"},
            )
        )
        self.assertEqual(config.url, "https://nas.example:5001")
        self.assertEqual(config.public_url, "https://callback.example")
        self.assertEqual(config.webhook_app_id, "app-2")
        self.assertEqual(config.otp_code, "123456")
        self.assertEqual(config.local_paths, {"/volume1": "/mnt/nas"})

    def test_missing_fields_are_rejected(self):
        for field, pattern in (
            ("url", "url"),
            ("webhook", "webhook"),
        ):
            settings = _settings()
            del settings[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, pattern):
                parse_synology_config(settings)

    def test_missing_callback_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "webhook.callback_url"):
            parse_synology_config(_settings(webhook={}))


def _context(*, offline: bool, mounts: dict[str, str] | None = None) -> BackendContext:
    storage = MagicMock()
    storage.register_schema = AsyncMock()
    return BackendContext(
        name="nas",
        mounts=mounts or {"p": "/volume1/p"},
        core=CoreServices(storage=storage, node_sync=MagicMock(), off_main=MagicMock()),
        routes=BackendRoutes(name="nas"),
        offline=offline,
    )


def _no_connection(*_: object, **__: object) -> None:
    raise AssertionError("must not connect")


class TestFactory(unittest.IsolatedAsyncioTestCase):
    async def test_offline_instance_does_not_connect(self):
        context = _context(offline=True)
        with patch(f"{_BACKEND}.create_synology_client", _no_connection):
            async with create_backend(
                context, **_settings(local_paths={"/volume1": "/mnt/nas"})
            ) as backend:
                self.assertEqual(
                    backend.local_path("/volume1/p/a.jpg"), Path("/mnt/nas/p/a.jpg")
                )
                self.assertIsNone(backend.local_path("/volume2/a.jpg"))
                with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                    await backend.command(["--help"])
        context.core.storage.register_schema.assert_awaited_once()

    async def test_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            async with create_backend(_context(offline=True)):
                pass

    async def test_backfill_connects_and_reconciles_this_instance(self):
        stats = dict.fromkeys(
            ("checked", "added", "updated", "removed", "list_errors"), 0
        )
        with (
            patch(f"{_BACKEND}.create_synology_client", return_value=_client()),
            patch(f"{_BACKEND}.create_mount_registry", AsyncMock()),
            patch(
                f"{_BACKEND}.BackfillService.run_virtual_path",
                AsyncMock(return_value=stats),
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            async with create_backend(_context(offline=True), **_settings()) as backend:
                await backend.command(["backfill", "/p", "--dry-run"])
        run.assert_awaited_once_with("/p", dry_run=True)

    async def test_serving_registers_its_webhook_route(self):
        client = MagicMock()
        client.list_webhooks = AsyncMock(return_value=[])
        client.create_webhook = AsyncMock(return_value="hook-1")
        client.delete_webhook = AsyncMock()
        context = _context(offline=False)
        with (
            patch(f"{_BACKEND}.create_synology_client", return_value=_client(client)),
            patch(f"{_BACKEND}.create_mount_registry", AsyncMock()),
        ):
            async with create_backend(context, **_settings()) as backend:
                self.assertIsNotNone(backend.operations)
                self.assertIsNotNone(context.routes.find("POST", "webhook"))
        client.create_webhook.assert_awaited_once_with(
            url="https://public.example/api/v1/backends/nas/webhook",
            app_id="wcpan-drive-server-synology",
        )
        client.delete_webhook.assert_awaited_once()


_BACKEND = "wcpan.drive.server.synology.backend"


def _client(client: object | None = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client or MagicMock())
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

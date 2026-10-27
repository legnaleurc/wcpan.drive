"""Synology backend settings and instances."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.backend import (
    BackendContext,
    BackendRoutes,
    CoreServices,
    enter_backend,
)
from wcpan.drive.server.synology import create_backend
from wcpan.drive.server.synology.config import SynologyConfig


def _settings(**overrides):
    settings = {
        "url": "https://nas.example",
        "username": "user",
        "password": "secret",
        "webhook_callback_url": "https://public.example",
    }
    settings.update(overrides)
    return settings


class TestSettings(unittest.IsolatedAsyncioTestCase):
    async def _config(self, **settings: object) -> SynologyConfig:
        with patch(f"{_BACKEND}.SynologyBackend") as backend:
            async with enter_backend(create_backend, _context(offline=True), settings):
                pass
        return backend.call_args.kwargs["config"]

    async def test_defaults(self):
        config = await self._config(**_settings())
        self.assertEqual(config.webhook_app_id, "wcpan-drive-server-synology")
        self.assertIsNone(config.otp_code)
        self.assertEqual(config.local_paths, {})

    async def test_maps_settings(self):
        config = await self._config(
            **_settings(
                url="https://nas.example:5001",
                otp_code="123456",
                webhook_callback_url="https://callback.example",
                webhook_app_id="app-2",
                local_paths={"/volume1": "/mnt/nas"},
            )
        )
        self.assertEqual(config.url, "https://nas.example:5001")
        self.assertEqual(config.public_url, "https://callback.example")
        self.assertEqual(config.webhook_app_id, "app-2")
        self.assertEqual(config.otp_code, "123456")
        self.assertEqual(config.local_paths, {"/volume1": "/mnt/nas"})

    def test_missing_settings_are_rejected(self):
        for field in ("url", "webhook_callback_url"):
            settings = _settings()
            del settings[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                enter_backend(create_backend, _context(offline=True), settings)

    def test_nested_webhook_is_rejected(self):
        settings = _settings(webhook={"callback_url": "https://public.example"})
        del settings["webhook_callback_url"]
        with self.assertRaisesRegex(ValueError, "unknown settings: webhook"):
            enter_backend(create_backend, _context(offline=True), settings)


def _context(*, offline: bool, mounts: dict[str, str] | None = None) -> BackendContext:
    mirror = MagicMock()
    mirror.register_schema = AsyncMock()
    return BackendContext(
        name="nas",
        mounts=mounts or {"p": "/volume1/p"},
        core=CoreServices(mirror=mirror, node_sync=MagicMock(), off_main=MagicMock()),
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
        context.core.mirror.register_schema.assert_awaited_once()

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

"""Synology backend settings and instances."""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from decoy import Decoy, matchers
from wcpan.drive.server.backend import (
    BackendContext,
    BackendRoutes,
    CoreServices,
    enter_backend,
)
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology import create_backend
from wcpan.drive.server.synology._config import (
    SynologyConfig,
    create_synology_client,
)
from wcpan.drive.server.synology._mounts import MountRegistry, create_mount_registry
from wcpan.synology import SynologyClient

from ._lib import DecoyTestCase


def _settings(**overrides):
    settings = {
        "url": "https://nas.example",
        "username": "user",
        "password": "secret",
        "webhook_callback_url": "https://public.example",
    }
    settings.update(overrides)
    return settings


class TestSettings(DecoyTestCase):
    async def _config(self, **settings: object) -> SynologyConfig:
        context = _context(offline=True, decoy=self.decoy)
        with patch(f"{_BACKEND}.SynologyBackend") as backend:
            async with enter_backend(create_backend, context, settings):
                pass
        return backend.call_args.kwargs["config"]

    async def test_defaults(self):
        # when
        config = await self._config(**_settings())
        # then
        self.assertEqual(config.webhook_app_id, "wcpan-drive-server-synology")
        self.assertIsNone(config.otp_code)
        self.assertEqual(config.local_paths, {})

    async def test_maps_settings(self):
        # when
        config = await self._config(
            **_settings(
                url="https://nas.example:5001",
                otp_code="123456",
                webhook_callback_url="https://callback.example",
                webhook_app_id="app-2",
                local_paths={"/volume1": "/mnt/nas"},
            )
        )
        # then
        self.assertEqual(config.url, "https://nas.example:5001")
        self.assertEqual(config.public_url, "https://callback.example")
        self.assertEqual(config.webhook_app_id, "app-2")
        self.assertEqual(config.otp_code, "123456")
        self.assertEqual(config.local_paths, {"/volume1": "/mnt/nas"})

    def test_missing_settings_are_rejected(self):
        # given
        for field in ("url", "webhook_callback_url"):
            settings = _settings()
            del settings[field]
            # when / then
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                enter_backend(
                    create_backend, _context(offline=True, decoy=self.decoy), settings
                )

    def test_nested_webhook_is_rejected(self):
        # given
        settings = _settings(webhook={"callback_url": "https://public.example"})
        del settings["webhook_callback_url"]
        # when / then
        with self.assertRaisesRegex(ValueError, "unknown settings: webhook"):
            enter_backend(
                create_backend, _context(offline=True, decoy=self.decoy), settings
            )


def _context(
    *, offline: bool, decoy: Decoy, mounts: dict[str, str] | None = None
) -> BackendContext:
    return BackendContext(
        name="nas",
        mounts=mounts or {"p": "/volume1/p"},
        core=CoreServices(
            mirror=decoy.mock(cls=MirrorStore),
            node_sync=decoy.mock(cls=NodeSyncService),
            off_main=decoy.mock(cls=OffMainService),
        ),
        routes=BackendRoutes(name="nas"),
        offline=offline,
    )


def _no_connection(*_: object, **__: object) -> None:
    raise AssertionError("must not connect")


class TestFactory(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.client = self.decoy.mock(cls=SynologyClient)
        connect = self.decoy.mock(func=create_synology_client)
        self.decoy.when(connect(matchers.Anything())).then_enter_with(self.client)
        resolve = self.decoy.mock(func=create_mount_registry)
        self.decoy.when(
            await resolve(matchers.Anything(), drive_api=self.client)
        ).then_return(MountRegistry(mounts={}, root_ids={}))
        for target, mock in (
            (f"{_BACKEND}.create_synology_client", connect),
            (f"{_BACKEND}.create_mount_registry", resolve),
        ):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_offline_instance_does_not_connect(self):
        # given
        context = _context(offline=True, decoy=self.decoy)
        with patch(f"{_BACKEND}.create_synology_client", _no_connection):
            # when
            async with create_backend(
                context, **_settings(local_paths={"/volume1": "/mnt/nas"})
            ) as backend:
                # then
                self.assertEqual(
                    backend.local_path("/volume1/p/a.jpg"), Path("/mnt/nas/p/a.jpg")
                )
                self.assertIsNone(backend.local_path("/volume2/a.jpg"))
                with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                    await backend.command(["--help"])
        self.decoy.verify(
            await context.core.mirror.register_schema(
                matchers.Anything(), matchers.Anything()
            ),
            times=1,
        )

    async def test_backfill_connects_and_reconciles_this_instance(self):
        # given
        stats = dict.fromkeys(
            ("checked", "added", "updated", "removed", "list_errors"), 0
        )
        context = _context(offline=True, decoy=self.decoy)
        with (
            patch(
                f"{_BACKEND}.BackfillService.run_virtual_path",
                AsyncMock(return_value=stats),
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            # when
            async with create_backend(context, **_settings()) as backend:
                await backend.command(["backfill", "/p", "--dry-run"])
        # then
        run.assert_awaited_once_with("/p", dry_run=True)

    async def test_serving_registers_its_webhook_route(self):
        # given
        app_id = "wcpan-drive-server-synology"
        self.decoy.when(await self.client.list_webhooks(app_id=app_id)).then_return([])
        self.decoy.when(
            await self.client.create_webhook(
                url="https://public.example/api/v1/backends/nas/webhook",
                app_id=app_id,
            )
        ).then_return("hook-1")
        context = _context(offline=False, decoy=self.decoy)
        # when
        async with create_backend(context, **_settings()) as backend:
            # then
            self.assertIsNotNone(backend.operations)
            self.assertIsNotNone(context.routes.find("POST", "webhook"))
        self.decoy.verify(
            await self.client.delete_webhook(webhook_id="hook-1", app_id=app_id),
            times=1,
        )


_BACKEND = "wcpan.drive.server.synology._backend"

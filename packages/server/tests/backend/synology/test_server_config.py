"""Synology backend settings and instances."""

import io
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from decoy import Decoy, matchers
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.backend.synology import create_backend
from wcpan.drive.server.backend.synology._commands import parse_command
from wcpan.drive.server.backend.synology._config import (
    SynologyConfig,
    create_synology_client,
)
from wcpan.drive.server.backend.synology._mounts import (
    MountRegistry,
    create_mount_registry,
)
from wcpan.drive.server.testing import ProbeRunner, enter_backend, load_command_parser
from wcpan.drive.server.types import (
    Access,
    BackendContext,
    BackendRoutes,
    CoreServices,
    MediaFailure,
    MediaProbing,
    MirrorReader,
    Mounts,
    NodeObservation,
    ProbeFailure,
)
from wcpan.synology.types import SynologyClient

from ._lib import ROOT_ID, DecoyTestCase, IdentityIds, make_mounts, mount_id


def _settings(**overrides):
    settings = {
        "url": "https://nas.example",
        "username": "user",
        "password": "secret",
        "webhook_callback_url": "https://public.example",
        "webhook_app_id": "1",
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
        self.assertIsNone(config.otp_code)

    async def test_maps_settings(self):
        # when
        config = await self._config(
            **_settings(
                url="https://nas.example:5001",
                otp_code="123456",
                webhook_callback_url="https://callback.example",
                webhook_app_id="app-2",
            )
        )
        # then
        self.assertEqual(config.url, "https://nas.example:5001")
        self.assertEqual(config.public_url, "https://callback.example")
        self.assertEqual(config.webhook_app_id, "app-2")
        self.assertEqual(config.otp_code, "123456")

    async def test_local_paths_setting_is_rejected(self):
        # when / then
        with self.assertRaisesRegex(ValueError, "local_paths"):
            await self._config(**_settings(local_paths={"/volume1": "/mnt/nas"}))

    def test_missing_settings_are_rejected(self):
        # given
        for field in ("url", "webhook_callback_url", "webhook_app_id"):
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
    node_sync: Any = MagicMock()
    node_sync.register_schema = AsyncMock(return_value=MagicMock())
    node_sync.enrichment_scope = MagicMock(side_effect=lambda: _no_retries())
    node_sync.retrying = MagicMock(side_effect=lambda _: _no_retries())
    return BackendContext(
        name="nas",
        mounts=make_mounts(mounts or {"p": "/volume1/p"}),
        core=CoreServices(
            media=MediaProbing(runner=ProbeRunner()),
            mirror=decoy.mock(cls=MirrorReader),
            node_sync=node_sync,
            ids=IdentityIds(),
        ),
        routes=BackendRoutes(name="nas"),
        access=Access.WRITE if offline else None,
    )


@asynccontextmanager
async def _no_retries() -> AsyncGenerator[None, None]:
    yield


def _no_connection(*_: object, **__: object) -> None:
    raise AssertionError("must not connect")


class TestCommands(DecoyTestCase):
    def test_commands_are_parsed_without_the_backend(self):
        # given
        parse = load_command_parser(create_backend)
        for argv in (["--help"], ["nope"], ["backfill"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                # when / then
                parse("nas", argv)

    def test_only_a_real_backfill_probes(self):
        # given
        parse = load_command_parser(create_backend)
        # when / then
        real = parse("nas", ["backfill", "/p"])
        self.assertEqual((real.access, real.probes), (Access.WRITE, True))
        for argv in (["backfill", "/p", "--dry-run"], ["pending"]):
            command = parse("nas", argv)
            self.assertEqual((command.access, command.probes), (Access.READ, False))


class TestFactory(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.client = self.decoy.mock(cls=SynologyClient)
        connect = self.decoy.mock(func=create_synology_client)
        self.decoy.when(connect(matchers.Anything())).then_enter_with(self.client)
        resolve = self.decoy.mock(func=create_mount_registry)
        self.decoy.when(
            await resolve(matchers.Anything(), drive_api=self.client)
        ).then_return(MountRegistry(mounts=Mounts(root_id=ROOT_ID), root_ids={}))
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
            async with create_backend(context, **_settings()):
                pass
        # then
        cast(Any, context.core.node_sync).register_schema.assert_awaited_once()

    async def test_backfill_connects_and_reconciles_this_instance(self):
        # given
        stats = dict.fromkeys(
            ("checked", "added", "updated", "removed", "list_errors"), 0
        )
        for argv, dry_run in ((["/p", "--dry-run"], True), (["/p"], False)):
            context = _context(offline=True, decoy=self.decoy)
            with (
                self.subTest(dry_run=dry_run),
                patch(
                    f"{_BACKEND}.BackfillService.run_virtual_path",
                    AsyncMock(return_value=stats),
                ) as run,
                redirect_stdout(io.StringIO()),
            ):
                # when
                async with create_backend(context, **_settings()) as backend:
                    await backend.run_command(parse_command("nas", ["backfill", *argv]))
                # then: only a real backfill runs the metadata workers
                run.assert_awaited_once_with("/p", dry_run=dry_run)

    async def test_pending_without_failures_does_not_connect(self):
        # given
        context = _context(offline=True, decoy=self.decoy)
        self.decoy.when(
            await context.core.mirror.get_media_failures("nas")
        ).then_return([])
        out = io.StringIO()
        with (
            patch(f"{_BACKEND}.create_synology_client", _no_connection),
            redirect_stdout(out),
        ):
            # when
            async with create_backend(context, **_settings()) as backend:
                await backend.run_command(parse_command("nas", ["pending"]))
        # then
        self.assertEqual(out.getvalue(), "")

    async def test_pending_lists_drive_paths(self):
        # given: one withheld file still on the Drive, one gone
        context = _context(offline=True, decoy=self.decoy)
        self.decoy.when(
            await context.core.mirror.get_media_failures("nas")
        ).then_return(
            [
                MediaFailure("link-1", ProbeFailure.TRANSIENT),
                MediaFailure("link-2", ProbeFailure.INVALID),
            ]
        )
        found = NodeObservation(
            record=NodeRecord(
                id=NodeId("link-1"),
                parent_id=mount_id("p"),
                name="a.jpg",
                is_directory=False,
                created_time=0,
                modified_time=0,
                changed_time=0,
                mime_type="image/jpeg",
                hash="",
                hash_algorithm="",
                size=1,
                is_image=True,
                is_video=False,
                width=0,
                height=0,
            ),
            locator=None,
        )

        async def locate_link(_self, _api, link):
            return found if link.permanent_link == "link-1" else None

        out = io.StringIO()
        with (
            patch(f"{_BACKEND}.SynologyPathService.locate_link", locate_link),
            redirect_stdout(out),
        ):
            # when
            async with create_backend(context, **_settings()) as backend:
                await backend.run_command(parse_command("nas", ["pending"]))

        # then
        self.assertEqual(
            out.getvalue(), "/volume1/p/a.jpg\ttransient\nlink-2\t(gone)\n"
        )

    async def test_serving_registers_its_webhook_route(self):
        # given
        app_id = "1"
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


_BACKEND = "wcpan.drive.server.backend.synology._backend"

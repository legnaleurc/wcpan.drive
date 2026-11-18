"""Filesystem backend settings."""

import io
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server._backend import (
    Access,
    BackendContext,
    BackendRoutes,
    CoreServices,
    Mounts,
    enter_backend,
    load_command_parser,
)
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import ProbeFailure
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.model import (
    MediaFailure,
)
from wcpan.drive.server.backend.filesystem import create_backend
from wcpan.drive.server.backend.filesystem._commands import parse_command
from wcpan.drive.server.backend.filesystem._config import FilesystemConfig, check_mounts
from wcpan.drive.server.backend.filesystem._handles import filesystem_id

from ._harness import make_mounts


class TestFilesystemSettings(TestCase):
    def test_defaults(self) -> None:
        # when
        config = FilesystemConfig()
        # then
        self.assertEqual(config.rescan_interval, 3600.0)
        self.assertTrue(config.watch)
        self.assertFalse(config.read_only)

    def test_rescan_interval_must_be_positive(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            FilesystemConfig(rescan_interval=0)

    def test_mounts_must_be_absolute(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            check_mounts(make_mounts({"docs": "relative/docs"}))

    def test_nested_mounts_are_rejected(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            check_mounts(make_mounts({"a": "/srv/data", "b": "/srv/data/inner/"}))

    def test_sibling_prefix_is_not_nested(self) -> None:
        # when / then
        self.assertEqual(
            _sources(check_mounts(make_mounts({"a": "/srv/data", "b": "/srv/data2/"}))),
            {"a": "/srv/data", "b": "/srv/data2"},
        )


def _sources(mounts: Mounts) -> dict[str, str]:
    return {name: m.source for name, m in mounts.items()}


_STORE = "wcpan.drive.server.backend.filesystem._commands.FilesystemStore"


def _context(mounts: dict[str, str]) -> BackendContext:
    node_sync = MagicMock()
    node_sync.register_schema = AsyncMock()
    return BackendContext(
        name="disk",
        mounts=make_mounts(mounts),
        core=CoreServices(
            media=MediaProbing(runner=ProbeRunner()),
            mirror=MagicMock(),
            node_sync=node_sync,
            off_main=MagicMock(),
            ids=MagicMock(),
        ),
        routes=BackendRoutes(name="disk"),
        access=Access.WRITE,
    )


class TestFactory(IsolatedAsyncioTestCase):
    async def test_settings_are_checked(self) -> None:
        # given
        context = _context({"docs": "/srv/docs"})
        # when / then
        async with enter_backend(
            create_backend, context, {"rescan_interval": 60, "watch": False}
        ):
            pass
        # when / then
        with self.assertRaisesRegex(ValueError, "watch must be bool"):
            enter_backend(create_backend, context, {"watch": "no"})
        with self.assertRaisesRegex(ValueError, "rescan_interval must be positive"):
            async with enter_backend(create_backend, context, {"rescan_interval": 0}):
                pass

    async def test_creates_instance_with_normalized_mounts(self) -> None:
        # when
        async with create_backend(
            _context({"docs": "/srv/docs/"}), rescan_interval=60
        ) as backend:
            # then
            self.assertEqual(_sources(backend.mounts), {"docs": "/srv/docs"})

    async def test_local_paths_setting_is_rejected(self) -> None:
        # when / then
        with self.assertRaisesRegex(ValueError, "local_paths"):
            enter_backend(
                create_backend,
                _context({"docs": "/srv/docs"}),
                {"local_paths": {"/srv": "/mnt/srv"}},
            )

    async def test_invalid_mounts_are_rejected(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            async with create_backend(_context({"docs": "relative"})):
                pass

    async def test_rebind_mount_records_current_filesystem(self) -> None:
        # given
        with tempfile.TemporaryDirectory() as tmp:
            for stored, writes in ((None, 1), ("old", 1), (filesystem_id(tmp), 0)):
                context = _context({"docs": tmp})
                out = io.StringIO()
                with (
                    self.subTest(stored=stored),
                    patch(
                        f"{_STORE}.get_mount_fsid", AsyncMock(return_value=stored)
                    ) as lookup,
                    patch(f"{_STORE}.set_mount_fsid", AsyncMock()) as record,
                    redirect_stdout(out),
                ):
                    # when
                    async with create_backend(context) as backend:
                        await backend.run_command(
                            parse_command("disk", ["rebind-mount", "docs"])
                        )
                    # then: an unchanged ID is not written again
                    lookup.assert_awaited_once_with("docs", tmp)
                    self.assertEqual(record.await_count, writes)
                    if writes:
                        record.assert_awaited_once_with("docs", tmp, filesystem_id(tmp))
                    else:
                        self.assertIn("already bound", out.getvalue())

    async def test_pending_lists_source_paths(self) -> None:
        # given
        context = _context({"docs": "/srv/docs"})
        context.core.mirror.get_media_failures = AsyncMock(
            return_value=[MediaFailure("/srv/docs/bad.jpg", ProbeFailure.LIMIT)]
        )
        out = io.StringIO()
        # when
        with redirect_stdout(out):
            async with create_backend(context) as backend:
                await backend.run_command(parse_command("disk", ["pending"]))
        # then
        self.assertEqual(out.getvalue(), "/srv/docs/bad.jpg\tlimit\n")
        context.core.mirror.get_media_failures.assert_awaited_once_with("disk")

    async def test_commands_are_parsed_without_the_backend(self) -> None:
        # when / then: the factory carries its parser
        parse = load_command_parser(create_backend)
        for argv in (["--help"], ["nope"], ["rebind-mount"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                parse("disk", argv)
        # when
        command = parse("disk", ["pending"])
        rebind = parse("disk", ["rebind-mount", "docs"])
        # then: pending only reads, rebind-mount writes, and neither probes
        self.assertEqual(command.access, Access.READ)
        self.assertFalse(command.probes)
        self.assertEqual(rebind.access, Access.WRITE)
        self.assertFalse(rebind.probes)

"""Filesystem backend settings."""

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.server.backend import (
    BackendContext,
    BackendRoutes,
    CoreServices,
    enter_backend,
)
from wcpan.drive.server.filesystem import create_backend
from wcpan.drive.server.filesystem._config import FilesystemConfig, check_mounts
from wcpan.drive.server.filesystem._handles import filesystem_id


class TestFilesystemSettings(TestCase):
    def test_defaults(self) -> None:
        # when
        config = FilesystemConfig()
        # then
        self.assertEqual(config.rescan_interval, 3600.0)
        self.assertTrue(config.watch)
        self.assertFalse(config.read_only)
        self.assertIsNone(config.local_paths)

    def test_rescan_interval_must_be_positive(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            FilesystemConfig(rescan_interval=0)

    def test_mounts_must_be_absolute(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            check_mounts({"docs": "relative/docs"})

    def test_nested_mounts_are_rejected(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            check_mounts({"a": "/srv/data", "b": "/srv/data/inner/"})

    def test_sibling_prefix_is_not_nested(self) -> None:
        # when / then
        self.assertEqual(
            check_mounts({"a": "/srv/data", "b": "/srv/data2/"}),
            {"a": "/srv/data", "b": "/srv/data2"},
        )


def _context(mounts: dict[str, str]) -> BackendContext:
    mirror = MagicMock()
    mirror.register_schema = AsyncMock()
    return BackendContext(
        name="disk",
        mounts=mounts,
        core=CoreServices(mirror=mirror, node_sync=MagicMock(), off_main=MagicMock()),
        routes=BackendRoutes(name="disk"),
        offline=True,
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
            self.assertEqual(backend.mounts, {"docs": "/srv/docs"})

    async def test_media_is_probed_at_source_paths_by_default(self) -> None:
        # when
        async with create_backend(_context({"docs": "/srv/docs"})) as backend:
            # then
            self.assertEqual(
                backend.local_path("/srv/docs/a.jpg"), Path("/srv/docs/a.jpg")
            )

    async def test_local_paths_remap_media_probing(self) -> None:
        # when
        async with create_backend(
            _context({"docs": "/srv/docs"}), local_paths={"/srv": "/mnt/srv"}
        ) as backend:
            # then
            self.assertEqual(
                backend.local_path("/srv/docs/a.jpg"), Path("/mnt/srv/docs/a.jpg")
            )

    async def test_invalid_mounts_are_rejected(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            async with create_backend(_context({"docs": "relative"})):
                pass

    async def test_rebind_mount_records_current_filesystem(self) -> None:
        # given
        with tempfile.TemporaryDirectory() as tmp:
            context = _context({"docs": tmp})
            with (
                patch(
                    "wcpan.drive.server.filesystem._commands.FilesystemStore.set_mount_fsid",
                    AsyncMock(),
                ) as record,
                redirect_stdout(io.StringIO()),
            ):
                # when
                async with create_backend(context) as backend:
                    await backend.command(["rebind-mount", "docs"])
            # then
            record.assert_awaited_once_with("docs", tmp, filesystem_id(tmp))

    async def test_command_help_does_not_start_anything(self) -> None:
        # given
        async with create_backend(_context({"docs": "/srv/docs"})) as backend:
            # when / then
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                await backend.command(["--help"])
            with self.assertRaises(RuntimeError):
                backend.operations

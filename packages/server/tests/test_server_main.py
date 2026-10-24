"""The ``wcpan.drive.server`` command loads backend factories named in the config."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml
from wcpan.drive.server.main import main

from . import _fake_backend


_FACTORY = "tests._fake_backend.create_backend"


def _config(tmp: str, **backend: object) -> str:
    raw = {
        "version": 3,
        "server": {"database_url": str(Path(tmp) / "mirror.db")},
        "backends": {"one": {"type": _FACTORY, **backend}},
        "mounts": {"docs": {"backend": "one", "source": "/srv/docs"}},
    }
    path = Path(tmp) / "server.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


class TestMain(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int | None, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        with redirect_stderr(stderr), redirect_stdout(stdout):
            try:
                main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def test_version_mismatch_stops_before_any_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.yaml"
            path.write_text("database_url: /tmp/x.db\n")
            with patch("wcpan.drive.server.main.cleanup_dangling_nodes") as cleanup:
                code, _, err = self.run_main(["--config", str(path), "gc"])
        self.assertEqual(code, 1)
        cleanup.assert_not_called()
        self.assertIn("config version mismatch", err)

    def test_missing_backend_package_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp, type="wcpan.drive.server.nonexistent.create_backend")
            code, _, err = self.run_main(["--config", path, "serve"])
        self.assertEqual(code, 1)
        self.assertIn("is its package installed", err)

    def test_backend_command_gets_the_rest_of_the_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            captured: list[list[str]] = []
            real = _fake_backend.FakeBackend.command

            async def record(self: _fake_backend.FakeBackend, argv: list[str]) -> None:
                assert self.context.offline
                captured.append(list(argv))
                await real(self, argv)

            with patch.object(_fake_backend.FakeBackend, "command", record):
                code, _, _ = self.run_main(
                    ["--config", path, "backend", "one", "poke", "--flag", "x"]
                )
        self.assertIsNone(code)
        self.assertEqual(captured, [["poke", "--flag", "x"]])

    def test_backend_help_is_left_to_the_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            captured: list[list[str]] = []

            async def record(self: _fake_backend.FakeBackend, argv: list[str]) -> None:
                captured.append(list(argv))

            with patch.object(_fake_backend.FakeBackend, "command", record):
                self.run_main(["--config", path, "backend", "one", "--help"])
        self.assertEqual(captured, [["--help"]])

    def test_unknown_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            code, _, err = self.run_main(["--config", path, "backend", "two", "x"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_backend_help_lists_configured_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            code, out, _ = self.run_main(["--config", path, "backend", "--help"])
        self.assertEqual(code, 0)
        self.assertIn(f"one ({_FACTORY})", out)

    def test_invalid_settings_name_the_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp, invalid=True)
            code, _, err = self.run_main(["--config", path, "backend", "one", "x"])
        self.assertEqual(code, 1)
        self.assertIn("backends.one: invalid setting", err)

    def test_help_works_without_config(self):
        code, _, _ = self.run_main(["--config", "/nonexistent.yaml", "--help"])
        self.assertEqual(code, 0)

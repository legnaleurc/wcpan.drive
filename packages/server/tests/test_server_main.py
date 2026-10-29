"""The ``wcpan.drive.server`` command loads backend factories named in the config."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import patch

import yaml
from wcpan.drive.server.config import parse_config
from wcpan.drive.server.main import main

from . import _fake_backend


_FACTORY = "tests._fake_backend.create_backend"
_PLUGINS = [
    EntryPoint("typed", "tests._fake_backend:create_typed_backend", "x"),
    EntryPoint("open", "tests._fake_backend:create_backend", "x"),
]


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


def _run_main(argv: list[str]) -> tuple[int | None, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code: int | None = None
    with redirect_stderr(stderr), redirect_stdout(stdout):
        try:
            main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


class TestMain(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int | None, str, str]:
        return _run_main(argv)

    def test_version_mismatch_stops_before_any_command(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.yaml"
            path.write_text("database_url: /tmp/x.db\n")
            with patch("wcpan.drive.server.main.cleanup_dangling_nodes") as cleanup:
                # when
                code, _, err = self.run_main(["--config", str(path), "gc"])
        # then
        self.assertEqual(code, 1)
        cleanup.assert_not_called()
        self.assertIn("config version mismatch", err)

    def test_missing_backend_package_is_reported(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp, type="wcpan.drive.server.nonexistent.create_backend")
            # when
            code, _, err = self.run_main(["--config", path, "serve"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("is its package installed", err)

    def test_backend_command_gets_the_rest_of_the_arguments(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            captured: list[list[str]] = []
            real = _fake_backend.FakeBackend.command

            async def record(self: _fake_backend.FakeBackend, argv: list[str]) -> None:
                assert self.context.offline
                captured.append(list(argv))
                await real(self, argv)

            with patch.object(_fake_backend.FakeBackend, "command", record):
                # when
                code, _, _ = self.run_main(
                    ["--config", path, "backend", "one", "poke", "--flag", "x"]
                )
        # then
        self.assertIsNone(code)
        self.assertEqual(captured, [["poke", "--flag", "x"]])

    def test_backend_help_is_left_to_the_backend(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            captured: list[list[str]] = []

            async def record(self: _fake_backend.FakeBackend, argv: list[str]) -> None:
                captured.append(list(argv))

            with patch.object(_fake_backend.FakeBackend, "command", record):
                # when
                self.run_main(["--config", path, "backend", "one", "--help"])
        # then
        self.assertEqual(captured, [["--help"]])

    def test_unknown_instance_is_rejected(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            # when
            code, _, err = self.run_main(["--config", path, "backend", "two", "x"])
        # then
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_backend_help_lists_configured_instances(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            # when
            code, out, _ = self.run_main(["--config", path, "backend", "--help"])
        # then
        self.assertEqual(code, 0)
        self.assertIn(f"one ({_FACTORY})", out)

    def test_invalid_settings_name_the_instance(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp, invalid=True)
            # when
            code, _, err = self.run_main(["--config", path, "backend", "one", "x"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("backends.one: invalid setting", err)

    def test_help_works_without_config(self):
        # when
        code, _, _ = self.run_main(["--config", "/nonexistent.yaml", "--help"])
        # then
        self.assertEqual(code, 0)

    def test_settings_are_checked_against_the_factory(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(
                tmp, type="tests._fake_backend.create_typed_backend", depth="2"
            )
            # when
            code, _, err = self.run_main(["--config", path, "backend", "one", "x"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("backends.one: missing settings: root", err)
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(
                tmp,
                type="tests._fake_backend.create_typed_backend",
                root="/srv",
                depth="2",
            )
            # when
            code, _, err = self.run_main(["--config", path, "backend", "one", "x"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("backends.one: depth must be int", err)


class TestInit(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch(
                "wcpan.drive.plugin._discovery.entry_points",
                lambda group: _PLUGINS,
            )
        )

    def run_main(self, argv: list[str]) -> tuple[int | None, str, str]:
        return _run_main(argv)

    def test_lists_installed_plugins(self):
        # when
        code, out, _ = self.run_main(["init", "--list"])
        # then
        self.assertIsNone(code)
        self.assertEqual(
            out.splitlines(),
            [
                "open\ttests._fake_backend.create_backend",
                "typed\ttests._fake_backend.create_typed_backend",
            ],
        )

    def test_scaffold_loads_once_required_values_are_filled(self):
        # when
        # A config that does not exist or is broken is never read.
        code, out, _ = self.run_main(
            ["--config", "/nonexistent.yaml", "init", "-p", "typed", "-p", "open"]
        )
        # then
        self.assertIsNone(code)
        self.assertIn("    root:  # required\n", out)
        self.assertIn("    # depth: 1\n", out)
        raw = yaml.safe_load(out)
        self.assertIsNone(raw["backends"]["typed"]["root"])
        # given
        raw["server"]["database_url"] = "/tmp/mirror.db"
        raw["backends"]["typed"]["root"] = "/srv"
        for mount in raw["mounts"].values():
            mount["source"] = "/srv"
        # when
        config = parse_config(raw)
        # then
        self.assertEqual(set(config.backends), {"typed", "open"})
        self.assertEqual(config.backends["typed"].settings, {"root": "/srv"})
        self.assertEqual(config.mounts["open"].backend, "open")

    def test_unknown_plugin_is_reported(self):
        # when
        code, _, err = self.run_main(["init", "-p", "nothing"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("is its package installed", err)

    def test_duplicate_plugin_is_rejected(self):
        # when
        code, _, err = self.run_main(["init", "-p", "typed", "-p", "typed"])
        # then
        self.assertEqual(code, 1)
        self.assertIn("given twice", err)

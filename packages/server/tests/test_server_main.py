"""The ``wcpan.drive.server`` command loads backend factories named in the config."""

import asyncio
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import patch

import yaml
from wcpan.drive.server._backend import Access, BackendCommand
from wcpan.drive.server._config import parse_config
from wcpan.drive.server._main import main
from wcpan.drive.server._services.mirror.instances import (
    registered_instances,
)
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService

from . import _fake_backend


_FACTORY = "tests._fake_backend.create_backend"
_PLUGINS = [
    EntryPoint("typed", "tests._fake_backend:create_typed_backend", "x"),
    EntryPoint("open", "tests._fake_backend:create_backend", "x"),
]


def _config(tmp: str, **backend: object) -> str:
    raw = {
        "version": 1,
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


def _register(tmp: str, *names: str) -> str:
    """Register instances *names* in the config's database; returns its path."""
    db = str(Path(tmp) / "mirror.db")

    async def register() -> None:
        with ThreadPoolExecutor() as pool:
            mirror = MirrorStore(db, off_main=OffMainService(pool=pool))
            await mirror.ensure_schema()
            for name in names:
                await mirror.register_instance(name)

    asyncio.run(register())
    return db


class TestLifecycleCommands(unittest.TestCase):
    def test_rebuild_a_configured_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            db = _register(tmp, "one")
            # when
            code, out, _ = _run_main(["--config", path, "backend", "one", "rebuild"])
            # then
            self.assertIsNone(code)
            self.assertIn("rediscovered on the next start", out)
            self.assertEqual(registered_instances(db), ["one"])

    def test_drop_refuses_a_configured_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            db = _register(tmp, "one")
            # when
            code, _, err = _run_main(["--config", path, "backend", "one", "drop"])
            # then
            self.assertEqual(code, 1)
            self.assertIn("remove it from the config first", err)
            self.assertEqual(registered_instances(db), ["one"])

    def test_drop_an_instance_no_longer_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            db = _register(tmp, "one", "gone")
            # when
            code, out, _ = _run_main(["--config", path, "backend", "gone", "drop"])
            # then
            self.assertIsNone(code)
            self.assertIn("Dropped backend 'gone'", out)
            self.assertEqual(registered_instances(db), ["one"])

    def test_unconfigured_instance_accepts_only_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            _register(tmp, "gone")
            for argv in (["backfill", "/"], ["rebuild"]):
                with self.subTest(argv=argv):
                    # when
                    code, _, err = _run_main(
                        ["--config", path, "backend", "gone", *argv]
                    )
                    # then
                    self.assertEqual(code, 1)
                    self.assertIn("drop", err)

    def test_help_lists_instances_no_longer_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            _register(tmp, "gone")
            # when
            code, out, _ = _run_main(["--config", path, "backend", "--help"])
        # then
        self.assertEqual(code, 0)
        self.assertIn("gone (not configured)", " ".join(out.split()))


class TestMain(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int | None, str, str]:
        return _run_main(argv)

    def test_version_mismatch_stops_before_any_command(self):
        # given
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.yaml"
            path.write_text("database_url: /tmp/x.db\n")
            with patch("wcpan.drive.server._main.cleanup_dangling_nodes") as cleanup:
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
            real = _fake_backend.FakeBackend.run_command

            async def record(
                self: _fake_backend.FakeBackend, command: BackendCommand
            ) -> None:
                assert self.context.offline
                captured.append(command.args.argv)
                await real(self, command)

            with patch.object(_fake_backend.FakeBackend, "run_command", record):
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
            _fake_backend.parsed.clear()
            # when
            code, out, _ = self.run_main(["--config", path, "backend", "one", "--help"])
        # then
        self.assertEqual(code, 0)
        self.assertEqual(_fake_backend.parsed, [["--help"]])
        self.assertIn("usage: backend one", out)

    def test_backend_help_starts_nothing(self):
        # given: no database yet, and a log file under the same directory
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            raw = yaml.safe_load(Path(path).read_text())
            raw["server"]["log_path"] = str(Path(tmp) / "server.log")
            Path(path).write_text(yaml.safe_dump(raw))
            # when
            with patch("wcpan.drive.server._main.dictConfig") as configure:
                code, _, _ = self.run_main(
                    ["--config", path, "backend", "one", "--help"]
                )
            # then
            self.assertEqual(code, 0)
            configure.assert_not_called()
            self.assertEqual(
                sorted(p.name for p in Path(tmp).iterdir()), ["server.yaml"]
            )

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


def _config_with_missing(tmp: str) -> str:
    """Instance ``one`` loads; instance ``two`` names a package not installed."""
    raw = {
        "version": 1,
        "server": {"database_url": str(Path(tmp) / "mirror.db")},
        "backends": {
            "one": {"type": _FACTORY},
            "two": {"type": "wcpan.drive.server.nonexistent.create_backend"},
        },
        "mounts": {
            "docs": {"backend": "one", "source": "/srv/docs"},
            "more": {"backend": "two", "source": "/srv/more"},
        },
    }
    path = Path(tmp) / "server.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


class TestLazyStartup(unittest.TestCase):
    """Commands import only the backends they run and parse without the DB."""

    def test_missing_backend_package_fails_only_what_needs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config_with_missing(tmp)
            _register(tmp, "one", "two", "gone")
            for argv in (
                ["gc"],
                ["squash"],
                ["backend", "one", "rebuild"],
                ["backend", "gone", "drop"],
                ["backend", "one", "poke"],
            ):
                with self.subTest(argv=argv):
                    # when
                    code, _, err = _run_main(["--config", path, *argv])
                    # then
                    self.assertIsNone(code, err)
            for argv in (["serve"], ["backend", "two", "poke"]):
                with self.subTest(argv=argv):
                    # when
                    code, _, err = _run_main(["--config", path, *argv])
                    # then
                    self.assertEqual(code, 1)
                    self.assertIn("is its package installed", err)

    def test_parsing_reads_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            _register(tmp, "one")
            with patch(
                "wcpan.drive.server._main.registered_instances",
                side_effect=AssertionError("read the database"),
            ):
                for argv in (["--help"], ["gc", "--help"], ["squash", "--help"]):
                    with self.subTest(argv=argv):
                        # when
                        code, _, err = _run_main(["--config", path, *argv])
                        # then
                        self.assertEqual(code, 0, err)
                # when: a configured instance needs no registry lookup
                code, _, err = _run_main(["--config", path, "backend", "one", "x"])
                # then
                self.assertIsNone(code, err)


class TestReadOnlyCommand(unittest.TestCase):
    """A read-only backend command writes no database file."""

    def test_refuses_a_missing_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            # when
            code, _, err = _run_main(
                ["--config", path, "backend", "one", "peek", "--read"]
            )
            # then
            self.assertEqual(code, 1)
            self.assertIn("start the server to create it", err)
            self.assertFalse((Path(tmp) / "mirror.db").exists())

    def test_runs_on_a_set_up_database_without_changing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            # given: a server start registered the instance and its schema
            code, _, err = _run_main(["--config", path, "backend", "one", "setup"])
            self.assertIsNone(code, err)
            code, _, err = _run_main(["--config", path, "checkpoint"])
            self.assertIsNone(code, err)
            before = {
                p.name: p.read_bytes()
                for p in Path(tmp).iterdir()
                if p.name.startswith("mirror.db")
            }
            captured: list[object] = []
            real = _fake_backend.FakeBackend.run_command

            async def record(
                self: _fake_backend.FakeBackend, command: BackendCommand
            ) -> None:
                captured.append(self.context.access)
                await real(self, command)

            # when
            with patch.object(_fake_backend.FakeBackend, "run_command", record):
                code, _, err = _run_main(
                    ["--config", path, "backend", "one", "peek", "--read"]
                )
            # then
            self.assertIsNone(code, err)
            self.assertEqual(captured, [Access.READ])
            after = {
                p.name: p.read_bytes()
                for p in Path(tmp).iterdir()
                if p.name.startswith("mirror.db")
            }
            self.assertEqual(after, before)


class TestCheckpointCommand(unittest.TestCase):
    def test_checkpoints_the_configured_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            _register(tmp, "one")
            # when
            code, out, err = _run_main(["--config", path, "checkpoint"])
            # then
            self.assertIsNone(code, err)
            self.assertIn("Checkpointed", out)

    def test_missing_database_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _config(tmp)
            # when
            code, _, err = _run_main(["--config", path, "checkpoint"])
            # then
            self.assertEqual(code, 1)
            self.assertIn("start the server to create it", err)
            self.assertFalse((Path(tmp) / "mirror.db").exists())


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

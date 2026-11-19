"""Every backend command declared read-only leaves the database files unchanged.

Each one runs through ``wcpan.drive.server`` against a set-up mirror, with no
WAL files and with the zero-byte WAL and leftover shared-memory file a
``checkpoint`` can leave. In a writable directory, the database, ``-wal``, and
``-shm`` must keep their names, sizes, and bytes afterwards. With the
directory and those files read-only, any attempt to write, even one that
would leave the same bytes or remove what it created, fails the command.
"""

import asyncio
import io
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import (
    asynccontextmanager,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from wcpan.drive.server._app import entered_backends, managed_core, managed_off_main
from wcpan.drive.server._backend import Access, load_command_parser
from wcpan.drive.server._config import parse_config
from wcpan.drive.server._main import main
from wcpan.drive.server._services.mounts import Mounts
from wcpan.drive.server._services.sync.mounts import ensure_mount_nodes
from wcpan.drive.server._types import ProbeFailure
from wcpan.drive.server.backend.filesystem import create_backend as create_filesystem
from wcpan.drive.server.backend.synology import create_backend as create_synology
from wcpan.drive.server.backend.synology._mounts import MountRegistry
from wcpan.synology.types import SynologyFileId


_SYNOLOGY = "wcpan.drive.server.backend.synology._backend"

# Every read-only command, with the arguments to run it and what it prints:
# each goes through its real conversion, so new IDs are needed but not saved.
_READ_COMMANDS: dict[str, list[tuple[list[str], str]]] = {
    "disk": [(["pending"], "/docs/a.jpg\tlimit\n")],
    "nas": [
        (["pending"], "/volume1/photos/a.jpg\tinvalid\n"),
        (
            ["backfill", "/", "--dry-run"],
            "Checked 1 node(s), added 1, updated 0, removed 0, list errors 0.\n",
        ),
    ],
}

_FACTORIES = {"disk": create_filesystem, "nas": create_synology}


def _raw_config(root: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "server": {"database_url": str(root / "db" / "mirror.db")},
        "backends": {
            "disk": {
                "type": "wcpan.drive.server.backend.filesystem.create_backend",
                "watch": False,
            },
            "nas": {
                "type": "wcpan.drive.server.backend.synology.create_backend",
                "url": "https://nas.example",
                "username": "user",
                "password": "secret",
                "webhook_callback_url": "https://public.example",
                "webhook_app_id": "1",
            },
        },
        "mounts": {
            "docs": {"backend": "disk", "source": str(root / "docs")},
            "photos": {"backend": "nas", "source": "/volume1/photos"},
        },
    }


async def _set_up(config_path: Path) -> None:
    """What a server start leaves behind, plus one withheld file per instance."""
    with open(config_path) as f:
        config = parse_config(yaml.safe_load(f))
    async with (
        managed_off_main() as off_main,
        managed_core(config, off_main=off_main, probes=False) as core,
        entered_backends(config, core, offline=True),
    ):
        await ensure_mount_nodes(
            core.mounts, mirror=core.mirror, node_sync=core.node_sync
        )
        await core.mirror.mark_media_failure("disk", "/docs/a.jpg", ProbeFailure.LIMIT)
        await core.mirror.mark_media_failure("nas", "link-1", ProbeFailure.INVALID)
        await core.write_queue.join()


def _leave_truncated_wal(db: str) -> None:
    """A zero-byte ``-wal`` and a ``-shm`` left by a process that exited."""
    code = (
        "import os, sqlite3\n"
        f"c = sqlite3.connect({db!r}, autocommit=True)\n"
        "c.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


@contextmanager
def _read_only(directory: Path, enabled: bool) -> Iterator[None]:
    """Make *directory* and its files read-only, so any write attempt fails."""
    if not enabled:
        yield
        return
    modes = {path: path.stat().st_mode for path in [directory, *directory.iterdir()]}
    try:
        for path in modes:
            os.chmod(path, 0o555 if path == directory else 0o444)
        yield
    finally:
        # The directory first, so its files can be restored.
        for path, mode in modes.items():
            os.chmod(path, mode)


_TS = 1_704_067_200


def _file(file_id: str, name: str, parent_id: str = "") -> dict[str, Any]:
    return {
        "file_id": file_id,
        "permanent_link": file_id,
        "parent_id": parent_id,
        "name": name,
        "type": "file",
        "content_type": "file",
        "size": 1,
        "created_time": _TS,
        "modified_time": _TS,
        "change_time": _TS,
        "sync_id": 1,
    }


@asynccontextmanager
async def _fake_drive(_config: object) -> AsyncGenerator[MagicMock, None]:
    """A Drive where ``link-1`` (withheld) is a file at the mount's root."""
    drive = MagicMock()
    drive.get_file = AsyncMock(return_value=_file("link-1", "a.jpg", "root-photos"))
    yield drive


async def _listing(
    _paths: object, _api: object, _parent: object
) -> list[dict[str, Any]]:
    """One file the mirror has never seen, so the dry run needs a new ID."""
    return [_file("f-new", "new.txt")]


async def _registry(mounts: Mounts, *, drive_api: object) -> MountRegistry:
    photos = mounts.get("photos")
    assert photos is not None
    return MountRegistry(
        mounts=mounts, root_ids={SynologyFileId("root-photos"): photos.id}
    )


class TestReadOnlyCommands(TestCase):
    def test_every_read_command_is_covered(self) -> None:
        for name, factory in _FACTORIES.items():
            parse = load_command_parser(factory)
            covered = {argv[0] for argv, _ in _READ_COMMANDS[name]}
            parser_choices = _subcommands(name, parse)
            for sub in parser_choices - covered:
                with self.subTest(backend=name, command=sub):
                    self.assertIsNot(_access(parse, name, sub), Access.READ)
            for argv, _ in _READ_COMMANDS[name]:
                with self.subTest(backend=name, argv=argv):
                    self.assertIs(parse(name, argv).access, Access.READ)

    def test_read_commands_write_no_database_file(self) -> None:
        for wal in (False, True):
            for read_only_dir in (False, True):
                for name, commands in _READ_COMMANDS.items():
                    for argv, expected in commands:
                        with self.subTest(
                            backend=name, argv=argv, wal=wal, read_only=read_only_dir
                        ):
                            self._check(
                                name,
                                argv,
                                expected,
                                wal=wal,
                                read_only_dir=read_only_dir,
                            )

    def _check(
        self,
        name: str,
        argv: list[str],
        expected: str,
        *,
        wal: bool,
        read_only_dir: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "db").mkdir()
            (root / "docs").mkdir()
            config_path = root / "server.yaml"
            config_path.write_text(yaml.safe_dump(_raw_config(root)))
            asyncio.run(_set_up(config_path))
            db_dir = root / "db"
            db = str(db_dir / "mirror.db")
            if wal:
                _leave_truncated_wal(db)
                self.assertEqual(os.path.getsize(f"{db}-wal"), 0)
            before = _snapshot(db_dir)
            out, err = io.StringIO(), io.StringIO()
            with (
                _read_only(db_dir, read_only_dir),
                patch(f"{_SYNOLOGY}.create_synology_client", _fake_drive),
                patch(f"{_SYNOLOGY}.create_mount_registry", _registry),
                patch(f"{_SYNOLOGY}.SynologyPathService.list_children", _listing),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                try:
                    main(["--config", str(config_path), "backend", name, *argv])
                except SystemExit as e:
                    self.fail(f"exited with {e.code}: {err.getvalue()}")
            self.assertEqual(_snapshot(db_dir), before)
            self.assertEqual(out.getvalue(), expected, err.getvalue())


def _subcommands(name: str, parse: Any) -> set[str]:
    """The subcommand names a backend's parser accepts, from its ``--help``."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        try:
            parse(name, ["--help"])
        except SystemExit:
            pass
    text = out.getvalue()
    start = text.index("{") + 1
    return set(text[start : text.index("}", start)].split(","))


def _access(parse: Any, name: str, sub: str) -> Access | None:
    """The access *sub* declares with placeholder arguments, if it parses."""
    for argv in ([sub], [sub, "x"]):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                return parse(name, argv).access
            except SystemExit:
                continue
    return None

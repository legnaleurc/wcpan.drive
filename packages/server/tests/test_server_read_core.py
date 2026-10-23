"""Read-only commands open the mirror without writing any database file."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server._app import (
    entered_read_backend,
    managed_core,
    managed_off_main,
    managed_read_core,
)
from wcpan.drive.server._backend import Access, ReadCoreServices
from wcpan.drive.server._config import parse_config
from wcpan.drive.server._lib.write_queue import (
    create_write_queue,
    write_worker,
)
from wcpan.drive.server._services.ids import NodeIds, ProvisionalNodeIds
from wcpan.drive.server._services.media.probing import ProbesNotDeclaredError
from wcpan.drive.server._services.mirror.model import (
    BackendNamespace,
    MirrorNotReadyError,
    SchemaVersionError,
)
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._types import ServerConfig
from wcpan.drive.sqlite import read_only_dsn

from . import _fake_backend


_FACTORY = "tests._fake_backend.create_backend"


def _config(tmp: str) -> ServerConfig:
    return parse_config(
        {
            "version": 1,
            "server": {"database_url": str(Path(tmp) / "mirror.db")},
            "backends": {"one": {"type": _FACTORY}},
            "mounts": {"docs": {"backend": "one", "source": "/srv/docs"}},
        }
    )


def _clear_metadata(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM metadata")


def _snapshot(directory: str) -> dict[str, bytes]:
    return {
        name: Path(directory, name).read_bytes()
        for name in sorted(os.listdir(directory))
        if name.startswith("mirror.db")
    }


async def _serve_once(config: ServerConfig) -> None:
    """Set the database up as a server start would, for instance ``one``."""
    async with (
        managed_off_main() as off_main,
        managed_core(config, off_main=off_main, probes=False) as core,
    ):
        await core.mirror.register_schema("one", _fake_backend.FakeSchema())


class ReadCoreTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.config = _config(self.tmp)
        self.db = self.config.database_url

    def sql(self, statement: str) -> None:
        con = sqlite3.connect(self.db, autocommit=True)
        try:
            con.execute(statement)
        finally:
            con.close()

    async def open(self) -> None:
        async with (
            managed_off_main() as off_main,
            managed_read_core(self.config, off_main=off_main) as core,
            entered_read_backend(self.config, core, "one") as backend,
        ):
            self.assertIsInstance(backend, _fake_backend.FakeBackend)


class TestRefusals(ReadCoreTestCase):
    async def test_missing_database(self) -> None:
        with self.assertRaisesRegex(MirrorNotReadyError, "start the server"):
            await self.open()
        self.assertFalse(os.path.exists(self.db))

    async def test_wal_left_by_a_crash(self) -> None:
        # given
        await _serve_once(self.config)
        code = (
            "import os, sqlite3\n"
            f"c = sqlite3.connect({self.db!r}, autocommit=True)\n"
            "c.execute('PRAGMA wal_autocheckpoint = 0')\n"
            "c.execute(\"INSERT INTO metadata (key, value) VALUES ('x', 'y')\")\n"
            "os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-I", "-c", code], check=True)
        before = _snapshot(self.tmp)
        # when / then
        with self.assertRaisesRegex(MirrorNotReadyError, "checkpoint") as raised:
            await self.open()
        self.assertIn("wal_checkpoint(TRUNCATE)", str(raised.exception))
        self.assertEqual(_snapshot(self.tmp), before)

    async def test_pending_compatibility_changes(self) -> None:
        for change in (
            "ALTER TABLE nodes ADD COLUMN ms_duration INTEGER",
            "CREATE TABLE pending_enrichment (node_id TEXT)",
            "ALTER TABLE media_failures DROP COLUMN kind",
            "DELETE FROM metadata WHERE key = 'epoch'",
            "DROP INDEX ix_nodes_parent_id",
            "PRAGMA journal_mode = DELETE",
        ):
            with self.subTest(change=change):
                # given
                if os.path.exists(self.db):
                    os.unlink(self.db)
                await _serve_once(self.config)
                self.sql(change)
                # when / then
                with self.assertRaisesRegex(MirrorNotReadyError, "checkpoint"):
                    await self.open()

    async def test_another_schema_version(self) -> None:
        # given
        await _serve_once(self.config)
        self.sql("PRAGMA user_version = 999")
        # when / then
        with self.assertRaises(SchemaVersionError):
            await self.open()

    async def test_unregistered_instance(self) -> None:
        # given: a database the server made before instance ``one`` existed
        async with (
            managed_off_main() as off_main,
            managed_core(self.config, off_main=off_main, probes=False),
        ):
            pass
        # when / then
        with self.assertRaisesRegex(MirrorNotReadyError, "not registered"):
            await self.open()

    async def test_unbound_and_switched_backends(self) -> None:
        # given
        await _serve_once(self.config)
        self.sql("UPDATE backend_instances SET schema = NULL")
        # when / then
        with self.assertRaisesRegex(MirrorNotReadyError, "no backend tables"):
            await self.open()
        # given
        self.sql("UPDATE backend_instances SET schema = 'other'")
        # when / then
        with self.assertRaisesRegex(SchemaVersionError, "holds rows of"):
            await self.open()
        # given
        self.sql("UPDATE backend_instances SET schema = 'fake'")
        self.sql("UPDATE backend_schemas SET version = 9 WHERE name = 'fake'")
        # when / then
        with self.assertRaisesRegex(SchemaVersionError, "version mismatch"):
            await self.open()


class TestReadCore(ReadCoreTestCase):
    async def test_opens_without_writing_any_file(self) -> None:
        # given
        await _serve_once(self.config)
        before = _snapshot(self.tmp)
        captured: list[ReadCoreServices] = []
        # when
        async with (
            managed_off_main() as off_main,
            managed_read_core(self.config, off_main=off_main) as core,
            entered_read_backend(self.config, core, "one") as backend,
        ):
            context = _fake_backend.instances["one"].context
            self.assertIs(context.access, Access.READ)
            assert isinstance(context.core, ReadCoreServices)
            captured.append(context.core)
            failures = await context.core.mirror.get_media_failures("one")
            ns = core.mirror.namespace("one")
            # then: writes through the store's connections are refused
            with self.assertRaises(sqlite3.OperationalError):
                await core.mirror.write(_clear_metadata)
            with self.assertRaises(ProbesNotDeclaredError):
                await context.core.media.check()
            del backend
        # then
        self.assertEqual(failures, [])
        self.assertIsInstance(ns, BackendNamespace)
        self.assertEqual(_snapshot(self.tmp), before)

    async def test_without_probes_a_write_core_cannot_probe(self) -> None:
        # given
        async with (
            managed_off_main() as off_main,
            managed_core(self.config, off_main=off_main, probes=False) as core,
        ):
            # when / then
            with self.assertRaises(ProbesNotDeclaredError):
                await core.media.check()


class TestProvisionalNodeIds(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = str(Path(tmp) / "mirror.db")
        self.pool = self.enterContext(ThreadPoolExecutor())
        off_main = OffMainService(pool=self.pool)
        writer = MirrorStore(self.db, off_main=off_main)
        await writer.ensure_schema()
        self.key = await writer.register_instance("one")
        queue = create_write_queue()
        worker = asyncio.create_task(write_worker(queue))
        self.mapped = (
            await NodeIds(mirror=writer, write_queue=queue, key=self.key).public(
                ["known"]
            )
        )[0]
        worker.cancel()
        reader = MirrorStore(read_only_dsn(self.db), off_main=off_main)
        self.ids = ProvisionalNodeIds(mirror=reader, key=self.key)

    def rows(self) -> int:
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
        finally:
            con.close()

    async def test_mapped_ids_are_kept_and_new_ones_are_not_persisted(self) -> None:
        # when
        known, new = await self.ids.public(["known", "new"])
        # then
        self.assertEqual(known, self.mapped)
        self.assertNotEqual(new, self.mapped)
        self.assertEqual(await self.ids.native_of(new), "new")
        self.assertEqual(await self.ids.public(["new"]), [new])
        self.assertEqual(await self.ids.lookup("new"), new)
        self.assertEqual(self.rows(), 1)

    async def test_concurrent_calls_agree(self) -> None:
        # when
        results = await asyncio.gather(*(self.ids.public(["x"]) for _ in range(8)))
        # then
        self.assertEqual(len({ids[0] for ids in results}), 1)

    async def test_an_unknown_public_id_is_rejected(self) -> None:
        # when / then
        with self.assertRaises(LookupError):
            await self.ids.native_of(NodeId("nope"))

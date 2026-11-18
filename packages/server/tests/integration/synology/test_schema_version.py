"""The mirror refuses a database with Synology tables of another version."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server._services.mirror.model import (
    SchemaVersionError,
)
from wcpan.drive.server._services.mirror.schema import (
    SCHEMA_VERSION,
)
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server.backend.synology._convert import SynologyLocator
from wcpan.drive.server.backend.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.synology.types import SynologyPath


class TestEnsureSchemaVersion(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        self.off_main = OffMainService(pool=self.pool)

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    async def test_new_db_sets_user_version(self) -> None:
        # when
        await MirrorStore(
            self.db_path, off_main=self.off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        ).ensure_schema()

        # then
        con = sqlite3.connect(self.db_path)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()

        self.assertEqual(version, SCHEMA_VERSION)

    async def test_other_version_db_is_refused(self) -> None:
        # given: a database at another schema version
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE nodes (id TEXT PRIMARY KEY);
            CREATE TABLE synology_locators (node_id TEXT PRIMARY KEY, file_id TEXT);
            PRAGMA user_version = 2;
            """
        )
        con.commit()
        con.close()

        # when / then
        with self.assertRaisesRegex(SchemaVersionError, "move this one aside"):
            await MirrorStore(
                self.db_path,
                off_main=self.off_main,
                schemas={"synology": SYNOLOGY_SCHEMA},
            ).ensure_schema()

    async def test_existing_db_with_matching_version_is_accepted(self) -> None:
        # given
        mirror = MirrorStore(
            self.db_path, off_main=self.off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        )
        await mirror.ensure_schema()
        # when / then
        await mirror.ensure_schema()

    async def test_existing_unversioned_db_is_rejected(self) -> None:
        # given
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

        # when / then
        with self.assertRaises(SchemaVersionError):
            await MirrorStore(
                self.db_path,
                off_main=self.off_main,
                schemas={"synology": SYNOLOGY_SCHEMA},
            ).ensure_schema()

    async def test_existing_db_with_wrong_version_is_rejected(self) -> None:
        # given
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA user_version = 99")
        con.commit()
        con.close()

        # when / then
        with self.assertRaises(SchemaVersionError):
            await MirrorStore(
                self.db_path,
                off_main=self.off_main,
                schemas={"synology": SYNOLOGY_SCHEMA},
            ).ensure_schema()

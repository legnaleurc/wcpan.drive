"""Tests for mount max_id tracking and DB schema versioning."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.services._schema import SCHEMA_VERSION
from wcpan.drive.server.services.mirror import (
    MirrorStore,
    SchemaVersionError,
)
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.synology import SynologyPath


class TestGetMountMaxIds(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"synology": SYNOLOGY_SCHEMA},
        )
        await self.mirror.ensure_schema()
        self.store = SynologyStore(
            mirror=self.mirror, ns=self.mirror.namespace("synology")
        )

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    def _write_mount(self, name: str, max_id: int, path: str) -> None:
        key = self.mirror.namespace("synology").key
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO synology_mounts (instance, name, max_id, path)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(instance, name) DO UPDATE SET"
            " max_id = excluded.max_id,"
            " path = excluded.path",
            (key, name, max_id, path),
        )
        con.commit()
        con.close()

    async def test_fresh_db_returns_zero_for_all_mounts(self) -> None:
        # given
        mounts = {
            "LV": SynologyPath("/team-folders/video/L"),
            "LG": SynologyPath("/team-folders/gallery/L"),
        }
        # when
        result = await self.store.get_mount_max_ids(mounts)
        # then
        self.assertEqual(result, {"LV": 0, "LG": 0})

    async def test_unknown_mount_gets_zero(self) -> None:
        # given
        self._write_mount("LV", 35000, "/team-folders/video/L")
        mounts = {
            "LV": SynologyPath("/team-folders/video/L"),
            "LG": SynologyPath("/team-folders/gallery/L"),
        }
        # when
        result = await self.store.get_mount_max_ids(mounts)
        # then
        self.assertEqual(result, {"LV": 35000, "LG": 0})

    async def test_path_change_resets_to_zero(self) -> None:
        # given
        self._write_mount("LV", 35000, "/old/path")
        mounts = {"LV": SynologyPath("/new/path")}
        # when
        result = await self.store.get_mount_max_ids(mounts)
        # then
        self.assertEqual(result["LV"], 0)

    async def test_matching_path_preserves_value(self) -> None:
        # given
        self._write_mount("LV", 35000, "/team-folders/video/L")
        mounts = {"LV": SynologyPath("/team-folders/video/L")}
        # when
        result = await self.store.get_mount_max_ids(mounts)
        # then
        self.assertEqual(result["LV"], 35000)


class TestSetMountState(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"synology": SYNOLOGY_SCHEMA},
        )
        await self.mirror.ensure_schema()
        self.store = SynologyStore(
            mirror=self.mirror, ns=self.mirror.namespace("synology")
        )

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    def _read_mount(self, name: str) -> tuple[int, str] | None:
        con = sqlite3.connect(self.db_path)
        key = self.mirror.namespace("synology").key
        row = con.execute(
            "SELECT max_id, path FROM synology_mounts WHERE instance = ? AND name = ?",
            (key, name),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    async def test_writes_mount_row(self) -> None:
        # when
        await self.store.set_mount_state("LV", "/team-folders/video/L", 35000)
        # then
        self.assertEqual(
            self._read_mount("LV"),
            (35000, "/team-folders/video/L"),
        )

    async def test_overwrites_existing_values(self) -> None:
        # given
        await self.store.set_mount_state("LV", "/old/path", 1000)
        # when
        await self.store.set_mount_state("LV", "/team-folders/video/L", 35000)
        # then
        self.assertEqual(
            self._read_mount("LV"),
            (35000, "/team-folders/video/L"),
        )

    async def test_roundtrip_via_get_mount_max_ids(self) -> None:
        # given
        mounts = {"LV": SynologyPath("/team-folders/video/L")}
        # when
        await self.store.set_mount_state("LV", "/team-folders/video/L", 35000)
        result = await self.store.get_mount_max_ids(mounts)
        # then
        self.assertEqual(result, {"LV": 35000})


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

    async def test_version_4_db_is_refused_not_migrated(self) -> None:
        # given: a database from before core-minted node IDs
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE nodes (id TEXT PRIMARY KEY);
            CREATE TABLE synology_locators (node_id TEXT PRIMARY KEY, file_id TEXT);
            PRAGMA user_version = 4;
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

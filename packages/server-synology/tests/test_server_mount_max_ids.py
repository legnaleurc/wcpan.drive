"""Tests for mount max_id tracking and DB schema versioning."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.services.mirror import (
    MirrorStore,
    SchemaVersionError,
)
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.synology.convert import SynologyLocator
from wcpan.drive.server.synology.store import SYNOLOGY_SCHEMA, SynologyStore
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
        self.store = SynologyStore(mirror=self.mirror)
        await self.mirror.ensure_schema()

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    def _write_mount(self, name: str, max_id: int, path: str) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO mounts (name, max_id, path) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET"
            " max_id = excluded.max_id,"
            " path = excluded.path",
            (name, max_id, path),
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
        self.store = SynologyStore(mirror=self.mirror)
        await self.mirror.ensure_schema()

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    def _read_mount(self, name: str) -> tuple[int, str] | None:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT max_id, path FROM mounts WHERE name = ?", (name,)
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

        self.assertEqual(version, 7)

    async def test_version_3_db_is_migrated(self) -> None:
        # given
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, mutable_id TEXT NOT NULL, parent_id TEXT,
                name TEXT NOT NULL, is_directory INTEGER NOT NULL DEFAULT 0,
                created_time INTEGER NOT NULL, modified_time INTEGER NOT NULL,
                changed_time INTEGER NOT NULL,
                mime_type TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                is_image INTEGER NOT NULL DEFAULT 0,
                is_video INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                ms_duration INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO nodes (id, mutable_id, name, is_directory, created_time,
                               modified_time, changed_time, hash)
            VALUES ('f1', 'm1', 'a.txt', 0, 0, 0, 0, 'abc'),
                   ('f2', 'm2', 'b.txt', 0, 0, 0, 0, ''),
                   ('d1', 'm3', 'dir', 1, 0, 0, 0, 'xyz');
            PRAGMA user_version = 3;
            """
        )
        con.commit()
        con.close()

        # when
        await MirrorStore(
            self.db_path, off_main=self.off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        ).ensure_schema()

        # then
        con = sqlite3.connect(self.db_path)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        rows = {
            row[0]: (row[1], row[2])
            for row in con.execute("SELECT id, hash, hash_algorithm FROM nodes")
        }
        locators = dict(con.execute("SELECT node_id, file_id FROM synology_locators"))
        con.close()

        self.assertEqual(version, 7)
        self.assertEqual(rows["f1"], ("abc", "md4"))
        self.assertEqual(rows["f2"], ("", ""))
        self.assertEqual(rows["d1"], ("", ""))
        self.assertEqual(locators, {"f1": "m1", "f2": "m2", "d1": "m3"})

    async def test_version_6_pending_entries_belong_to_legacy_backend(self) -> None:
        # given
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, parent_id TEXT,
                name TEXT NOT NULL, is_directory INTEGER NOT NULL DEFAULT 0,
                created_time INTEGER NOT NULL, modified_time INTEGER NOT NULL,
                changed_time INTEGER NOT NULL,
                mime_type TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '',
                hash_algorithm TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                is_image INTEGER NOT NULL DEFAULT 0,
                is_video INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                ms_duration INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE changes (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                is_removed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE pending_enrichment (node_id TEXT PRIMARY KEY);
            INSERT INTO pending_enrichment (node_id) VALUES ('pending-1');
            PRAGMA user_version = 6;
            """
        )
        con.commit()
        con.close()

        # when
        mirror = MirrorStore(self.db_path, off_main=self.off_main)
        await mirror.ensure_schema()

        # then
        self.assertEqual(await mirror.get_pending_enrichment("synology"), ["pending-1"])
        self.assertEqual(await mirror.get_pending_enrichment("other"), [])

    async def test_version_4_db_moves_locators_out_of_nodes(self) -> None:
        # given
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, mutable_id TEXT NOT NULL, parent_id TEXT,
                name TEXT NOT NULL, is_directory INTEGER NOT NULL DEFAULT 0,
                created_time INTEGER NOT NULL, modified_time INTEGER NOT NULL,
                changed_time INTEGER NOT NULL,
                mime_type TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '',
                hash_algorithm TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                is_image INTEGER NOT NULL DEFAULT 0,
                is_video INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                ms_duration INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX ix_nodes_parent_id ON nodes(parent_id);
            CREATE INDEX ix_nodes_mutable_id ON nodes(mutable_id);
            CREATE TABLE changes (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                is_removed INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO nodes (id, mutable_id, parent_id, name, is_directory,
                               created_time, modified_time, changed_time)
            VALUES ('_', '', NULL, '', 1, 0, 0, 0),
                   ('_docs', '', '_', 'docs', 1, 0, 0, 0),
                   ('perm-d', '101', '_docs', 'dir', 1, 0, 0, 0),
                   ('perm-f', '102', 'perm-d', 'a.txt', 0, 0, 0, 0);
            INSERT INTO changes (node_id, is_removed)
            VALUES ('_', 0), ('_docs', 0), ('perm-d', 0), ('perm-f', 0);
            PRAGMA user_version = 4;
            """
        )
        con.commit()
        con.close()

        # when
        mirror = MirrorStore(
            self.db_path, off_main=self.off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        )
        await mirror.ensure_schema()

        # then
        con = sqlite3.connect(self.db_path)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in con.execute("PRAGMA table_info(nodes)")}
        con.close()
        self.assertEqual(version, 7)
        self.assertNotIn("mutable_id", columns)

        # Public IDs and the change feed survive; virtual nodes get no locator.
        self.assertEqual(
            await SynologyStore(mirror=mirror).get_locators(
                ["_", "_docs", "perm-d", "perm-f"]
            ),
            {"perm-d": "101", "perm-f": "102"},
        )
        found = await SynologyStore(mirror=mirror).get_node_by_locator(
            SynologyLocator("102")
        )
        assert found is not None
        self.assertEqual((found.id, found.parent_id), ("perm-f", "perm-d"))
        changes, cursor, _ = await mirror.get_changes_since(0, 100)
        self.assertEqual([c[0] for c in changes], ["_", "_docs", "perm-d", "perm-f"])
        self.assertEqual(cursor, 4)

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

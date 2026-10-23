"""Tests for mount max_id tracking and DB schema versioning."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.storage import (
    SchemaVersionError,
    StorageService,
)
from wcpan.synology import SynologyPath


class TestGetMountMaxIds(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        self.storage = StorageService(
            self.db_path, off_main=OffMainService(pool=self.pool)
        )
        await self.storage.ensure_schema()

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
        mounts = {
            "LV": SynologyPath("/team-folders/video/L"),
            "LG": SynologyPath("/team-folders/gallery/L"),
        }
        result = await self.storage.get_mount_max_ids(mounts)
        self.assertEqual(result, {"LV": 0, "LG": 0})

    async def test_unknown_mount_gets_zero(self) -> None:
        self._write_mount("LV", 35000, "/team-folders/video/L")
        mounts = {
            "LV": SynologyPath("/team-folders/video/L"),
            "LG": SynologyPath("/team-folders/gallery/L"),
        }
        result = await self.storage.get_mount_max_ids(mounts)
        self.assertEqual(result, {"LV": 35000, "LG": 0})

    async def test_path_change_resets_to_zero(self) -> None:
        self._write_mount("LV", 35000, "/old/path")
        mounts = {"LV": SynologyPath("/new/path")}
        result = await self.storage.get_mount_max_ids(mounts)
        self.assertEqual(result["LV"], 0)

    async def test_matching_path_preserves_value(self) -> None:
        self._write_mount("LV", 35000, "/team-folders/video/L")
        mounts = {"LV": SynologyPath("/team-folders/video/L")}
        result = await self.storage.get_mount_max_ids(mounts)
        self.assertEqual(result["LV"], 35000)


class TestSetMountState(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor()
        self.storage = StorageService(
            self.db_path, off_main=OffMainService(pool=self.pool)
        )
        await self.storage.ensure_schema()

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
        await self.storage.set_mount_state("LV", "/team-folders/video/L", 35000)
        self.assertEqual(
            self._read_mount("LV"),
            (35000, "/team-folders/video/L"),
        )

    async def test_overwrites_existing_values(self) -> None:
        await self.storage.set_mount_state("LV", "/old/path", 1000)
        await self.storage.set_mount_state("LV", "/team-folders/video/L", 35000)
        self.assertEqual(
            self._read_mount("LV"),
            (35000, "/team-folders/video/L"),
        )

    async def test_roundtrip_via_get_mount_max_ids(self) -> None:
        mounts = {"LV": SynologyPath("/team-folders/video/L")}
        await self.storage.set_mount_state("LV", "/team-folders/video/L", 35000)
        result = await self.storage.get_mount_max_ids(mounts)
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
        await StorageService(self.db_path, off_main=self.off_main).ensure_schema()

        con = sqlite3.connect(self.db_path)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()

        self.assertEqual(version, 3)

    async def test_existing_db_with_matching_version_is_accepted(self) -> None:
        storage = StorageService(self.db_path, off_main=self.off_main)
        await storage.ensure_schema()
        await storage.ensure_schema()

    async def test_existing_unversioned_db_is_rejected(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

        with self.assertRaises(SchemaVersionError):
            await StorageService(self.db_path, off_main=self.off_main).ensure_schema()

    async def test_existing_db_with_wrong_version_is_rejected(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA user_version = 99")
        con.commit()
        con.close()

        with self.assertRaises(SchemaVersionError):
            await StorageService(self.db_path, off_main=self.off_main).ensure_schema()

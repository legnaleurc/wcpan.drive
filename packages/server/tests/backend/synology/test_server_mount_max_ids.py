"""Tests for mount max_id tracking and DB schema versioning."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.backend.synology._convert import SynologyLocator
from wcpan.drive.server.backend.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.testing import backend_harness
from wcpan.synology.types import SynologyPath


class TestGetMountMaxIds(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        server = await self.enterAsyncContext(backend_harness())
        instance = await server.add_instance("synology", SYNOLOGY_SCHEMA, {})
        self.mirror = server.mirror
        self.db_path = str(server.database_path)
        self.store = SynologyStore(
            mirror=self.mirror, ns=instance.namespace, write=self.mirror.write
        )

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
        server = await self.enterAsyncContext(backend_harness())
        instance = await server.add_instance("synology", SYNOLOGY_SCHEMA, {})
        self.mirror = server.mirror
        self.db_path = str(server.database_path)
        self.store = SynologyStore(
            mirror=self.mirror, ns=instance.namespace, write=self.mirror.write
        )

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

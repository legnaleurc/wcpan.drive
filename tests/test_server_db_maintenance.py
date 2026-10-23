"""Tests for cleanup_dangling_nodes and reset_change_history."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.client.remote._lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import SERVER_ROOT_ID
from wcpan.drive.server.services.storage import (
    SchemaVersionError,
    StorageService,
    cleanup_dangling_nodes,
    reset_change_history,
)


_NOW = 1_704_067_200


def _make_node(
    node_id: str,
    *,
    parent_id: str | None = SERVER_ROOT_ID,
    name: str = "test.txt",
    is_directory: bool = False,
    is_image: bool = False,
    is_video: bool = False,
    width: int = 0,
    height: int = 0,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=_NOW,
        modified_time=_NOW,
        changed_time=_NOW,
        mime_type=FOLDER_MIME_TYPE if is_directory else "image/jpeg",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=is_image,
        is_video=is_video,
        width=width,
        height=height,
        ms_duration=0,
    )


def _make_storage(db_path: str, pool: ThreadPoolExecutor) -> StorageService:
    return StorageService(db_path, off_main=OffMainService(pool=pool))


class TestCleanupDanglingNodes(IsolatedAsyncioTestCase):
    def test_rejects_existing_unversioned_db(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
            con.commit()
            con.close()

            with self.assertRaises(SchemaVersionError):
                cleanup_dangling_nodes(db_path)
        finally:
            os.unlink(db_path)

    async def test_no_dangling_nodes_returns_zero(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                await storage.bulk_upsert_nodes([_make_node("node-001")])
                count = cleanup_dangling_nodes(db_path)
                self.assertEqual(count, 0)
                self.assertIsNotNone(await storage.get_node_by_id("node-001"))
        finally:
            os.unlink(db_path)

    async def test_removes_node_with_broken_parent(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await storage.bulk_upsert_nodes([orphan])
                count = cleanup_dangling_nodes(db_path)
                self.assertEqual(count, 1)
                self.assertIsNone(await storage.get_node_by_id("orphan-001"))
        finally:
            os.unlink(db_path)

    async def test_emits_removal_changes_for_dangling_nodes(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await storage.bulk_upsert_nodes([orphan])
                cleanup_dangling_nodes(db_path)
                changes, _, _ = await storage.get_changes_since(0, 1000)
                self.assertEqual(len(changes), 1)
                self.assertTrue(changes[0][1])
                self.assertEqual(changes[0][0], "orphan-001")
        finally:
            os.unlink(db_path)

    async def test_keeps_reachable_nodes(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                reachable = _make_node("node-001")
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await storage.bulk_upsert_nodes([reachable, orphan])
                count = cleanup_dangling_nodes(db_path)
                self.assertEqual(count, 1)
                self.assertIsNotNone(await storage.get_node_by_id("node-001"))
                self.assertIsNone(await storage.get_node_by_id("orphan-001"))
        finally:
            os.unlink(db_path)

    async def test_does_not_remove_server_root(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                root = _make_node(
                    SERVER_ROOT_ID,
                    parent_id=None,
                    name="",
                    is_directory=True,
                )
                await storage.bulk_upsert_nodes([root])
                cleanup_dangling_nodes(db_path)
                self.assertIsNotNone(await storage.get_node_by_id(SERVER_ROOT_ID))
        finally:
            os.unlink(db_path)


class TestResetChangeHistory(IsolatedAsyncioTestCase):
    async def test_clears_existing_changes(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                node = _make_node("node-001")
                await storage.bulk_upsert_nodes([node])
                await storage.upsert_node_and_emit_change(node)
                await storage.upsert_node_and_emit_change(node)
                await storage.upsert_node_and_emit_change(node)
                reset_change_history(db_path)
                changes, _, _ = await storage.get_changes_since(0, 1000)
                ids = [c[0] for c in changes]
                self.assertEqual(ids.count("node-001"), 1)
        finally:
            os.unlink(db_path)

    async def test_returns_count_of_inserted_records(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                await storage.bulk_upsert_nodes(
                    [_make_node("node-001"), _make_node("node-002")]
                )
                count = reset_change_history(db_path)
                self.assertEqual(count, 2)
        finally:
            os.unlink(db_path)

    async def test_server_root_not_included_in_changes(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                root = _make_node(
                    SERVER_ROOT_ID,
                    parent_id=None,
                    name="",
                    is_directory=True,
                )
                await storage.bulk_upsert_nodes([root, _make_node("node-001")])
                reset_change_history(db_path)
                changes, _, _ = await storage.get_changes_since(0, 1000)
                ids = [c[0] for c in changes]
                self.assertNotIn(SERVER_ROOT_ID, ids)
        finally:
            os.unlink(db_path)

    async def test_all_new_changes_are_updates(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                storage = _make_storage(db_path, pool)
                await storage.ensure_schema()
                await storage.bulk_upsert_nodes([_make_node("node-001")])
                reset_change_history(db_path)
                changes, _, _ = await storage.get_changes_since(0, 1000)
                self.assertTrue(all(not c[1] for c in changes))
        finally:
            os.unlink(db_path)

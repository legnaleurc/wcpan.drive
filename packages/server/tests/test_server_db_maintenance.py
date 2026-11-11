"""Tests for cleanup_dangling_nodes and reset_change_history."""

import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.mirror import (
    MirrorStore,
    SchemaVersionError,
    cleanup_dangling_nodes,
    reset_change_history,
)
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import SERVER_ROOT_ID


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


def _make_mirror(db_path: str, pool: ThreadPoolExecutor) -> MirrorStore:
    return MirrorStore(db_path, off_main=OffMainService(pool=pool))


class TestCleanupDanglingNodes(IsolatedAsyncioTestCase):
    def test_rejects_existing_unversioned_db(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
            con.commit()
            con.close()

            # when / then
            with self.assertRaises(SchemaVersionError):
                cleanup_dangling_nodes(db_path)
        finally:
            os.unlink(db_path)

    async def test_no_dangling_nodes_returns_zero(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes([_make_node("node-001")])
                # when
                count = cleanup_dangling_nodes(db_path)
                # then
                self.assertEqual(count, 0)
                self.assertIsNotNone(await mirror.get_node_by_id("node-001"))
        finally:
            os.unlink(db_path)

    async def test_removes_node_with_broken_parent(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await mirror.bulk_upsert_nodes([orphan])
                # when
                count = cleanup_dangling_nodes(db_path)
                # then
                self.assertEqual(count, 1)
                self.assertIsNone(await mirror.get_node_by_id("orphan-001"))
        finally:
            os.unlink(db_path)

    async def test_emits_removal_changes_for_dangling_nodes(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await mirror.bulk_upsert_nodes([orphan])
                # when
                cleanup_dangling_nodes(db_path)
                # then
                changes, _, _ = await mirror.get_changes_since(0, 1000)
                self.assertEqual(len(changes), 1)
                self.assertTrue(changes[0][1])
                self.assertEqual(changes[0][0], "orphan-001")
        finally:
            os.unlink(db_path)

    async def test_keeps_reachable_nodes(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                reachable = _make_node("node-001")
                orphan = _make_node("orphan-001", parent_id="nonexistent-parent")
                await mirror.bulk_upsert_nodes([reachable, orphan])
                # when
                count = cleanup_dangling_nodes(db_path)
                # then
                self.assertEqual(count, 1)
                self.assertIsNotNone(await mirror.get_node_by_id("node-001"))
                self.assertIsNone(await mirror.get_node_by_id("orphan-001"))
        finally:
            os.unlink(db_path)

    async def test_does_not_remove_server_root(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                root = _make_node(
                    SERVER_ROOT_ID,
                    parent_id=None,
                    name="",
                    is_directory=True,
                )
                await mirror.bulk_upsert_nodes([root])
                # when
                cleanup_dangling_nodes(db_path)
                # then
                self.assertIsNotNone(await mirror.get_node_by_id(SERVER_ROOT_ID))
        finally:
            os.unlink(db_path)


class TestResetChangeHistory(IsolatedAsyncioTestCase):
    async def test_clears_existing_changes(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                node = _make_node("node-001")
                await mirror.bulk_upsert_nodes([node])
                await mirror.upsert_node_and_emit_change(node)
                await mirror.upsert_node_and_emit_change(node)
                await mirror.upsert_node_and_emit_change(node)
                # when
                reset_change_history(db_path)
                # then
                changes, _, _ = await mirror.get_changes_since(0, 1000)
                ids = [c[0] for c in changes]
                self.assertEqual(ids.count("node-001"), 1)
        finally:
            os.unlink(db_path)

    async def test_returns_count_of_inserted_records(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes(
                    [_make_node("node-001"), _make_node("node-002")]
                )
                # when
                count = reset_change_history(db_path)
                # then
                self.assertEqual(count, 2)
        finally:
            os.unlink(db_path)

    async def test_server_root_not_included_in_changes(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                root = _make_node(
                    SERVER_ROOT_ID,
                    parent_id=None,
                    name="",
                    is_directory=True,
                )
                await mirror.bulk_upsert_nodes([root, _make_node("node-001")])
                # when
                reset_change_history(db_path)
                # then
                changes, _, _ = await mirror.get_changes_since(0, 1000)
                ids = [c[0] for c in changes]
                self.assertNotIn(SERVER_ROOT_ID, ids)
        finally:
            os.unlink(db_path)

    async def test_all_new_changes_are_updates(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                await mirror.bulk_upsert_nodes([_make_node("node-001")])
                # when
                reset_change_history(db_path)
                # then
                changes, _, _ = await mirror.get_changes_since(0, 1000)
                self.assertTrue(all(not c[1] for c in changes))
        finally:
            os.unlink(db_path)


class TestEpoch(IsolatedAsyncioTestCase):
    async def test_epoch_survives_reopen(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                first = _make_mirror(db_path, pool)
                await first.ensure_schema()
                # when
                second = _make_mirror(db_path, pool)
                await second.ensure_schema()
                # then
                self.assertTrue(first.epoch)
                self.assertEqual(second.epoch, first.epoch)
        finally:
            os.unlink(db_path)

    async def test_new_database_gets_new_epoch(self) -> None:
        # given
        epochs: list[str] = []
        with ThreadPoolExecutor() as pool:
            for _ in range(2):
                fd, db_path = tempfile.mkstemp(suffix=".sqlite")
                os.close(fd)
                try:
                    mirror = _make_mirror(db_path, pool)
                    # when
                    await mirror.ensure_schema()
                    epochs.append(mirror.epoch)
                finally:
                    os.unlink(db_path)
        # then
        self.assertNotEqual(epochs[0], epochs[1])

    async def test_reset_change_history_replaces_epoch(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                before = _make_mirror(db_path, pool)
                await before.ensure_schema()
                # when
                reset_change_history(db_path)
                # then
                after = _make_mirror(db_path, pool)
                await after.ensure_schema()
                self.assertNotEqual(after.epoch, before.epoch)
        finally:
            os.unlink(db_path)


class TestOlderDatabase(IsolatedAsyncioTestCase):
    def test_older_schema_is_refused_not_migrated(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
            con.execute("PRAGMA user_version = 4")
            con.commit()
            con.close()

            # when / then
            with self.assertRaisesRegex(SchemaVersionError, "move this one aside"):
                cleanup_dangling_nodes(db_path)
        finally:
            os.unlink(db_path)


class TestPerBackendGc(IsolatedAsyncioTestCase):
    """``gc`` keeps a node only under a mount served by the backend that owns it."""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(os.unlink, self.db_path)
        self.pool = self.enterContext(ThreadPoolExecutor())
        self.mirror = _make_mirror(self.db_path, self.pool)
        await self.mirror.ensure_schema()
        self.keys = {
            name: await self.mirror.register_backend(name) for name in ("a", "b")
        }
        await self.mirror.bulk_upsert_nodes(
            [
                _make_node(SERVER_ROOT_ID, parent_id=None, is_directory=True),
                _make_node("m-docs", name="docs", is_directory=True),
                _make_node("m-pics", name="pics", is_directory=True),
                _make_node("m-old", name="old", is_directory=True),
                # served by a under its own mount
                _make_node("a-dir", parent_id="m-docs", is_directory=True),
                _make_node("a-file", parent_id="a-dir"),
                # left by b under a mount a now serves
                _make_node("b-dir", parent_id="m-docs", is_directory=True),
                _make_node("b-file", parent_id="b-dir"),
                # a mount now served by an instance never started
                _make_node("a-pic", parent_id="m-pics"),
                # a mount no longer configured keeps its nodes
                _make_node("b-old", parent_id="m-old"),
            ]
        )
        self._own(
            a=["a-dir", "a-file", "a-pic", "a-unused", "a-withheld"],
            b=["b-dir", "b-file", "b-old"],
        )
        await self.mirror.mark_enrichment_pending("a-withheld", "a")

    def _own(self, **owned: list[str]) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.executemany(
                "INSERT INTO node_ids (id, backend, native) VALUES (?, ?, ?)",
                [
                    (node_id, self.keys[name], node_id)
                    for name, ids in owned.items()
                    for node_id in ids
                ],
            )
            con.commit()
        finally:
            con.close()

    def _mapped(self) -> set[str]:
        con = sqlite3.connect(self.db_path)
        try:
            return {row[0] for row in con.execute("SELECT id FROM node_ids")}
        finally:
            con.close()

    async def test_removes_nodes_left_by_another_backend(self) -> None:
        # when
        count = cleanup_dangling_nodes(
            self.db_path, mounts={"docs": "a", "pics": "new"}
        )

        # then
        self.assertEqual(count, 3)
        kept = await self.mirror.get_nodes_by_ids(
            ["a-dir", "a-file", "b-dir", "b-file", "a-pic", "b-old", "m-pics"]
        )
        self.assertEqual(set(kept), {"a-dir", "a-file", "b-old", "m-pics"})
        changes, _, _ = await self.mirror.get_changes_since(0, 1000)
        self.assertEqual({c[0] for c in changes if c[1]}, {"b-dir", "b-file", "a-pic"})

    async def test_prunes_unused_ids_but_keeps_withheld_ones(self) -> None:
        # when
        cleanup_dangling_nodes(self.db_path, mounts={"docs": "a", "pics": "new"})

        # then
        self.assertEqual(self._mapped(), {"a-dir", "a-file", "a-withheld", "b-old"})

    async def test_without_mounts_keeps_every_reachable_node(self) -> None:
        # when
        count = cleanup_dangling_nodes(self.db_path)

        # then
        self.assertEqual(count, 0)
        self.assertNotIn("a-unused", self._mapped())

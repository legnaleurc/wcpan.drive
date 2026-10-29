"""Replay-safety tests for mirror change paging semantics."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import SERVER_ROOT_ID


_NOW = 1_704_067_200


def _make_node(
    node_id: str,
    *,
    parent_id: str | None = SERVER_ROOT_ID,
    is_directory: bool = False,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=node_id,
        is_directory=is_directory,
        created_time=_NOW,
        modified_time=_NOW,
        changed_time=_NOW,
        mime_type=FOLDER_MIME_TYPE if is_directory else "text/plain",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _make_mirror(db_path: str, pool: ThreadPoolExecutor) -> MirrorStore:
    return MirrorStore(db_path, off_main=OffMainService(pool=pool))


class TestMirrorChangesPaging(IsolatedAsyncioTestCase):
    async def test_cursor_advances_by_raw_rows_even_with_same_node_repeats(
        self,
    ) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                n1 = _make_node("n1")
                await mirror.upsert_node_and_emit_change(n1)
                await mirror.upsert_node_and_emit_change(n1)
                await mirror.delete_subtree_and_emit_changes("n1")

                # when
                page1, cursor1, has_more1 = await mirror.get_changes_since(0, 2)
                # then
                self.assertEqual(len(page1), 1)
                self.assertTrue(has_more1)
                self.assertEqual(page1[0][0], "n1")
                self.assertFalse(page1[0][1])

                # when
                page2, cursor2, has_more2 = await mirror.get_changes_since(cursor1, 2)
                # then
                self.assertEqual(len(page2), 1)
                self.assertFalse(has_more2)
                self.assertEqual(page2[0][0], "n1")
                self.assertTrue(page2[0][1])
                self.assertGreater(cursor2, cursor1)
        finally:
            os.unlink(db_path)

    async def test_delete_then_recreate_same_node_is_replayable(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                n1 = _make_node("n1")
                await mirror.upsert_node_and_emit_change(n1)
                await mirror.delete_subtree_and_emit_changes("n1")
                await mirror.upsert_node_and_emit_change(n1)

                # when
                changes, _, _ = await mirror.get_changes_since(0, 10)
                # then
                self.assertEqual([c[0] for c in changes], ["n1"])
                self.assertEqual([c[1] for c in changes], [False])
        finally:
            os.unlink(db_path)

    async def test_same_page_upsert_then_delete_keeps_delete(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                n1 = _make_node("n1")
                await mirror.upsert_node_and_emit_change(n1)
                await mirror.delete_subtree_and_emit_changes("n1")

                # when
                changes, _, _ = await mirror.get_changes_since(0, 10)
                # then
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0][0], "n1")
                self.assertTrue(changes[0][1])
        finally:
            os.unlink(db_path)

    async def test_same_page_delete_then_upsert_keeps_upsert(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                n1 = _make_node("n1")
                await mirror.upsert_node_and_emit_change(n1)
                await mirror.delete_subtree_and_emit_changes("n1")
                await mirror.upsert_node_and_emit_change(n1)

                # when
                changes, _, _ = await mirror.get_changes_since(0, 10)
                # then
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0][0], "n1")
                self.assertFalse(changes[0][1])
        finally:
            os.unlink(db_path)

    async def test_subtree_delete_emits_parent_and_child_removals(self) -> None:
        # given
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            with ThreadPoolExecutor() as pool:
                mirror = _make_mirror(db_path, pool)
                await mirror.ensure_schema()
                parent = _make_node("parent", is_directory=True)
                child = _make_node("child", parent_id="parent")
                await mirror.bulk_upsert_nodes([parent, child])
                await mirror.delete_subtree_and_emit_changes("parent")

                # when
                changes, _, _ = await mirror.get_changes_since(0, 10)
                # then
                removed_ids = {
                    node_id for node_id, is_removed, _ in changes if is_removed
                }
                self.assertEqual(removed_ids, {"parent", "child"})
        finally:
            os.unlink(db_path)

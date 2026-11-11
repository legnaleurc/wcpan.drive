"""Synology locator persistence: private to the backend, bound atomically to nodes."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.lib.root import SERVER_ROOT_ID
from wcpan.drive.server.services.mirror import (
    MirrorStore,
    cleanup_dangling_nodes,
)
from wcpan.drive.server.services.mounts import mount_id
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.types import NodeObservation


_MOUNT = mount_id("docs")


def _node(node_id: str, parent_id: str | None, *, name: str = "") -> NodeRecord:
    return NodeRecord(
        id=NodeId(node_id),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
        name=name or node_id,
        is_directory=True,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type="application/x-directory",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _obs(record: NodeRecord, locator: str | None) -> NodeObservation:
    return NodeObservation(
        record=record,
        locator=SynologyLocator(locator) if locator is not None else None,
    )


class TestLocatorPersistence(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.pool = ThreadPoolExecutor(2)
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"synology": SYNOLOGY_SCHEMA},
        )
        self.store = SynologyStore(mirror=self.mirror)
        await self.mirror.ensure_schema()
        await self.mirror.bulk_upsert_nodes(
            [_node(SERVER_ROOT_ID, None), _node(_MOUNT, SERVER_ROOT_ID)]
        )

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    async def _change_ids(self) -> list[str]:
        changes, _, _ = await self.mirror.get_changes_since(0, 1000)
        return [c[0] for c in changes]

    async def test_node_and_locator_are_written_together(self) -> None:
        # when
        await self.mirror.apply_observations([_obs(_node("d", _MOUNT), "101")])

        # then
        self.assertEqual(await self.store.get_locators([NodeId("d")]), {"d": "101"})
        found = await self.store.get_node_by_locator(SynologyLocator("101"))
        assert found is not None
        self.assertEqual(found.id, "d")
        self.assertEqual(await self._change_ids(), ["d"])

    async def test_locator_only_update_emits_no_change(self) -> None:
        # given
        d = _node("d", _MOUNT)
        await self.mirror.apply_observations([_obs(d, "101")])
        _, cursor, _ = await self.mirror.get_changes_since(0, 1000)

        # when
        await self.mirror.apply_observations([_obs(d, "202")])

        # then
        self.assertEqual(await self.store.get_locators([d.id]), {"d": "202"})
        self.assertIsNone(await self.store.get_node_by_locator(SynologyLocator("101")))
        changes, _, _ = await self.mirror.get_changes_since(cursor, 1000)
        self.assertEqual(changes, [])

    async def test_public_change_with_same_locator_emits_change(self) -> None:
        # given
        d = _node("d", _MOUNT)
        await self.mirror.apply_observations([_obs(d, "101")])
        _, cursor, _ = await self.mirror.get_changes_since(0, 1000)

        # when
        await self.mirror.apply_observations([_obs(replace(d, name="renamed"), "101")])

        # then
        changes, _, _ = await self.mirror.get_changes_since(cursor, 1000)
        self.assertEqual([c[0] for c in changes], ["d"])

    async def test_none_locator_keeps_existing_binding(self) -> None:
        # given
        d = _node("d", _MOUNT)
        await self.mirror.apply_observations([_obs(d, "101")])

        # when
        await self.mirror.apply_observations([_obs(replace(d, name="renamed"), None)])

        # then
        self.assertEqual(await self.store.get_locators([d.id]), {"d": "101"})

    async def test_subtree_delete_removes_bindings(self) -> None:
        # given
        await self.mirror.apply_observations(
            [_obs(_node("d", _MOUNT), "101"), _obs(_node("e", "d"), "102")]
        )

        # when
        await self.mirror.delete_subtree_and_emit_changes(NodeId("d"))

        # then
        self.assertEqual(
            await self.store.get_locators([NodeId("d"), NodeId("e")]),
            {},
        )
        self.assertIsNone(await self.store.get_node_by_locator(SynologyLocator("102")))

    async def test_dangling_cleanup_removes_bindings(self) -> None:
        # given
        await self.mirror.apply_observations([_obs(_node("d", _MOUNT), "101")])
        # Detach "d" from the tree so the reachability GC treats it as dangling.
        await self.mirror.bulk_upsert_nodes([_node("d", "missing-parent")])

        # when / then
        self.assertEqual(
            cleanup_dangling_nodes(self.db_path, schemas={"synology": SYNOLOGY_SCHEMA}),
            1,
        )

        # then
        self.assertEqual(await self.store.get_locators([NodeId("d")]), {})

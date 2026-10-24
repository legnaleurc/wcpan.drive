"""Synology locator persistence: private to the backend, bound atomically to nodes."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID, mount_id
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.storage import (
    StorageService,
    cleanup_dangling_nodes,
)
from wcpan.drive.server.synology.convert import SynologyLocator
from wcpan.drive.server.synology.store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.types import NodeObservation


_MOUNT = mount_id("docs")


def _node(node_id: str, parent_id: str | None, *, name: str = "") -> NodeRecord:
    return NodeRecord(
        id=MirrorStableId(node_id),
        parent_id=MirrorStableId(parent_id) if parent_id is not None else None,
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
        self.storage = StorageService(
            self.db_path,
            off_main=OffMainService(pool=self.pool),
            schemas={"synology": SYNOLOGY_SCHEMA},
        )
        self.store = SynologyStore(storage=self.storage)
        await self.storage.ensure_schema()
        await self.storage.bulk_upsert_nodes(
            [_node(SERVER_ROOT_ID, None), _node(_MOUNT, SERVER_ROOT_ID)]
        )

    async def asyncTearDown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        os.unlink(self.db_path)

    async def _change_ids(self) -> list[str]:
        changes, _, _ = await self.storage.get_changes_since(0, 1000)
        return [c[0] for c in changes]

    async def test_node_and_locator_are_written_together(self) -> None:
        await self.storage.apply_observations([_obs(_node("d", _MOUNT), "101")])

        self.assertEqual(
            await self.store.get_locators([MirrorStableId("d")]), {"d": "101"}
        )
        found = await self.store.get_node_by_locator(SynologyLocator("101"))
        assert found is not None
        self.assertEqual(found.id, "d")
        self.assertEqual(await self._change_ids(), ["d"])

    async def test_locator_only_update_emits_no_change(self) -> None:
        d = _node("d", _MOUNT)
        await self.storage.apply_observations([_obs(d, "101")])
        _, cursor, _ = await self.storage.get_changes_since(0, 1000)

        await self.storage.apply_observations([_obs(d, "202")])

        self.assertEqual(await self.store.get_locators([d.id]), {"d": "202"})
        self.assertIsNone(await self.store.get_node_by_locator(SynologyLocator("101")))
        changes, _, _ = await self.storage.get_changes_since(cursor, 1000)
        self.assertEqual(changes, [])

    async def test_public_change_with_same_locator_emits_change(self) -> None:
        d = _node("d", _MOUNT)
        await self.storage.apply_observations([_obs(d, "101")])
        _, cursor, _ = await self.storage.get_changes_since(0, 1000)

        await self.storage.apply_observations([_obs(replace(d, name="renamed"), "101")])

        changes, _, _ = await self.storage.get_changes_since(cursor, 1000)
        self.assertEqual([c[0] for c in changes], ["d"])

    async def test_none_locator_keeps_existing_binding(self) -> None:
        d = _node("d", _MOUNT)
        await self.storage.apply_observations([_obs(d, "101")])

        await self.storage.apply_observations([_obs(replace(d, name="renamed"), None)])

        self.assertEqual(await self.store.get_locators([d.id]), {"d": "101"})

    async def test_subtree_delete_removes_bindings(self) -> None:
        await self.storage.apply_observations(
            [_obs(_node("d", _MOUNT), "101"), _obs(_node("e", "d"), "102")]
        )

        await self.storage.delete_subtree_and_emit_changes(MirrorStableId("d"))

        self.assertEqual(
            await self.store.get_locators([MirrorStableId("d"), MirrorStableId("e")]),
            {},
        )
        self.assertIsNone(await self.store.get_node_by_locator(SynologyLocator("102")))

    async def test_dangling_cleanup_removes_bindings(self) -> None:
        await self.storage.apply_observations([_obs(_node("d", _MOUNT), "101")])
        # Detach "d" from the tree so the reachability GC treats it as dangling.
        await self.storage.bulk_upsert_nodes([_node("d", "missing-parent")])

        self.assertEqual(
            cleanup_dangling_nodes(self.db_path, schemas={"synology": SYNOLOGY_SCHEMA}),
            1,
        )

        self.assertEqual(await self.store.get_locators([MirrorStableId("d")]), {})

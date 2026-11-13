"""Synology nodes get core-minted public IDs, isolated per instance."""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from decoy import matchers
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.lib.debounce import TaskIdDebouncer
from wcpan.drive.server.services.ids import NodeIds
from wcpan.drive.server.services.mirror import MirrorStore, rebuild_backend
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._operations import SynologyNodeOperations
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.synology._webhook import WebhookService
from wcpan.drive.server.types import ReadSeq, WriteQueue
from wcpan.drive.server.workers import (
    create_write_queue,
    write_worker,
)
from wcpan.synology import (
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyPermanentLink,
)

from ._lib import ROOT_ID, DecoyTestCase, make_mounts, mount_id


_READ = ReadSeq(1)


def _info(*, link: str = "link", file_id: str = "7") -> SynologyFileInfo:
    return {
        "file_id": file_id,
        "permanent_link": link,
        "parent_id": "root",
        "name": "a.txt",
        "type": "file",
        "content_type": "document",
        "size": 1,
        "created_time": 1,
        "modified_time": 1,
        "change_time": 1,
        "sync_id": 1,
    }


def _dir(node_id: str, parent_id: str | None, name: str) -> NodeRecord:
    return NodeRecord(
        id=NodeId(node_id),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
        name=name,
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


class _Instance:
    """One Synology instance's services over the shared mirror."""

    def __init__(
        self, mirror: MirrorStore, name: str, mount: str, *, write_queue: WriteQueue
    ) -> None:
        ns = mirror.namespace(name)
        self.name = name
        self.mount = mount_id(mount)
        self.store = SynologyStore(mirror=mirror, ns=ns)
        self.ids = NodeIds(mirror=mirror, write_queue=write_queue, key=ns.key)
        self.paths = SynologyPathService(
            registry=MountRegistry(
                mounts=make_mounts({mount: f"/{mount}"}),
                root_ids={SynologyFileId("root"): self.mount},
            ),
            store=self.store,
            ids=self.ids,
        )


class TestTwoInstances(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(os.unlink, self.db_path)
        pool = self.enterContext(ThreadPoolExecutor())
        self.mirror = MirrorStore(
            self.db_path,
            off_main=OffMainService(pool=pool),
            schemas={"nas1": SYNOLOGY_SCHEMA, "nas2": SYNOLOGY_SCHEMA},
        )
        await self.mirror.ensure_schema()
        self.write_queue = create_write_queue()
        worker = asyncio.create_task(write_worker(self.write_queue))
        self.addCleanup(worker.cancel)
        await self.mirror.bulk_upsert_nodes(
            [
                _dir(ROOT_ID, None, ""),
                _dir(mount_id("one"), ROOT_ID, "one"),
                _dir(mount_id("two"), ROOT_ID, "two"),
            ]
        )
        self.nas1 = _Instance(self.mirror, "nas1", "one", write_queue=self.write_queue)
        self.nas2 = _Instance(self.mirror, "nas2", "two", write_queue=self.write_queue)

    async def _mirror(self, instance: _Instance, info: SynologyFileInfo) -> NodeId:
        observation = await instance.paths.convert_one(info, instance.mount)
        assert observation is not None
        await self.mirror.apply_observations(
            [replace(observation, backend=instance.name)]
        )
        return observation.record.id

    async def test_same_native_ids_do_not_collide(self) -> None:
        # given: both NAS report the same permanent link and file_id
        # when
        in_1 = await self._mirror(self.nas1, _info())
        in_2 = await self._mirror(self.nas2, _info())

        # then
        self.assertNotEqual(in_1, in_2)
        self.assertNotIn(in_1, ("link", "7"))
        self.assertEqual(await self.mirror.owner_of(in_1), "nas1")
        self.assertEqual(await self.mirror.owner_of(in_2), "nas2")
        found_1 = await self.nas1.store.get_node_by_locator(SynologyLocator("7"))
        found_2 = await self.nas2.store.get_node_by_locator(SynologyLocator("7"))
        assert found_1 is not None and found_2 is not None
        self.assertEqual((found_1.id, found_2.id), (in_1, in_2))

    async def test_removing_a_node_leaves_the_other_instance_alone(self) -> None:
        # given
        in_1 = await self._mirror(self.nas1, _info())
        in_2 = await self._mirror(self.nas2, _info())

        # when
        await self.mirror.delete_subtree_and_emit_changes(in_1)

        # then
        self.assertIsNone(
            await self.nas1.store.get_node_by_locator(SynologyLocator("7"))
        )
        self.assertEqual(await self.nas2.store.get_locators([in_2]), {in_2: "7"})

    async def test_a_deleted_node_keeps_its_id(self) -> None:
        # given
        node_id = await self._mirror(self.nas1, _info())
        await self.mirror.delete_subtree_and_emit_changes(node_id)

        # when: a delayed observation of the same item
        again = await self.nas1.paths.convert_one(_info(), self.nas1.mount)

        # then: ordering, keyed by public ID, still sees the same node
        assert again is not None
        self.assertEqual(again.record.id, node_id)

    async def test_rebuild_restores_nodes_under_their_old_ids(self) -> None:
        # given
        node_id = await self._mirror(self.nas1, _info())
        other = await self._mirror(self.nas2, _info())

        # when: nas1 is rebuilt, and rediscovers the item after restarting
        rebuild_backend(self.db_path, "nas1")
        self.assertIsNone(await self.mirror.get_node_by_id(node_id))
        await self.mirror.ensure_schema()
        self.nas1 = _Instance(self.mirror, "nas1", "one", write_queue=self.write_queue)
        again = await self._mirror(self.nas1, _info())

        # then
        self.assertEqual(again, node_id)
        self.assertEqual(await self.mirror.owner_of(again), "nas1")
        self.assertIsNotNone(await self.mirror.get_node_by_id(other))

    async def test_locate_asks_for_the_permanent_link(self) -> None:
        # given
        node_id = await self._mirror(self.nas1, _info())
        drive_api = self.decoy.mock(cls=SynologyClient)
        self.decoy.when(
            await drive_api.get_file(SynologyPermanentLink("link"))
        ).then_return(_info())

        # when
        found = await self.nas1.paths.locate(drive_api, node_id)
        foreign = await self.nas2.paths.locate(drive_api, node_id)

        # then
        assert found is not None
        self.assertEqual(found.record.id, node_id)
        self.assertIsNone(foreign)

    async def test_webhook_removal_looks_up_without_minting(self) -> None:
        # given
        node_id = await self._mirror(self.nas1, _info())
        node_sync = self.decoy.mock(cls=NodeSyncService)
        self.decoy.when(node_sync.observe()).then_enter_with(_READ)
        service = WebhookService(
            drive_api=self.decoy.mock(cls=SynologyClient),
            mirror=self.mirror,
            store=self.nas1.store,
            node_sync=node_sync,
            syno_paths=self.nas1.paths,
            mount_registry=MountRegistry(mounts=Mounts(root_id=ROOT_ID), root_ids={}),
        )
        pending = self.decoy.mock(cls=TaskIdDebouncer)

        def removal(link: str):
            return service._classify_webhook_item(  # pyright: ignore[reportPrivateUsage]
                {
                    "event_type": "file_removed",
                    "file_id": "7",
                    "permanent_link": link,
                    "file_type": "file",
                    "parent_id": "root",
                }
            )

        # when
        for link in ("link", "never-seen"):
            await service._execute_webhook_plan(  # pyright: ignore[reportPrivateUsage]
                removal(link), pending=pending
            )

        # then
        self.decoy.verify(await node_sync.delete(node_id, read=_READ), times=1)
        self.decoy.verify(
            await node_sync.delete(matchers.Anything(), read=_READ), times=1
        )
        self.assertIsNone(await self.nas1.ids.lookup("never-seen"))

    async def test_move_keeps_the_node_when_its_link_is_unchanged(self) -> None:
        # given: a node mirrored under a folder of the mount
        folder_info = _info(link="folder-link", file_id="8")
        folder_info["type"] = "dir"
        folder = await self._mirror(self.nas1, folder_info)
        record = await self.mirror.get_node_by_id(
            await self._mirror(self.nas1, _info())
        )
        assert record is not None
        drive_api = self.decoy.mock(cls=SynologyClient)
        moved = {**_info(), "parent_id": "8"}
        self.decoy.when(
            await drive_api.get_file(SynologyPermanentLink("link"))
        ).then_return(moved)
        node_sync = self.decoy.mock(cls=NodeSyncService)
        self.decoy.when(node_sync.observe()).then_enter_with(_READ)
        operations = SynologyNodeOperations(
            drive_api=drive_api, syno_paths=self.nas1.paths, node_sync=node_sync
        )

        # when
        await operations.update(record, name=None, parent_id=folder)

        # then
        self.decoy.verify(
            await drive_api.move(SynologyPermanentLink("link"), SynologyFileId("8")),
            times=1,
        )
        self.decoy.verify(
            await node_sync.delete(matchers.Anything(), read=_READ), times=0
        )
        self.decoy.verify(
            await node_sync.upsert(
                matchers.HasAttributes(
                    {"record": matchers.HasAttributes({"id": record.id})}
                ),
                read=_READ,
            ),
            times=1,
        )

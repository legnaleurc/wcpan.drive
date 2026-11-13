"""Scan and backfill descend into folders whose writes have not landed yet.

Folders are listed by permanent link, which does not depend on the folder's
own write, unlike its stored ``file_id``.
"""

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from unittest.mock import patch

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.lib.root import SERVER_ROOT_ID
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology._backfill import BackfillService
from wcpan.drive.server.synology._mounts import MountRegistry
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._scan import StartupScanService
from wcpan.drive.server.synology._store import SYNOLOGY_SCHEMA, SynologyStore
from wcpan.drive.server.types import WriteQueue
from wcpan.drive.server.workers import create_write_queue, metadata_worker
from wcpan.synology import SynologyPath, SynologyPermanentLink

from ._lib import DecoyTestCase, IdentityIds, make_mounts, mount_id


def _dir(node_id: str, name: str, parent_id: str | None) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=True,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type=FOLDER_MIME_TYPE,
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


def _item(file_id: str, link: str, name: str, *, is_dir: bool) -> dict:
    return {
        "file_id": file_id,
        "permanent_link": link,
        "parent_id": "",
        "name": name,
        "type": "dir" if is_dir else "file",
        "content_type": "dir" if is_dir else "file",
        "size": 0,
        "created_time": 0,
        "modified_time": 0,
        "change_time": 0,
        "sync_id": 1,
        "max_id": 1,
    }


class _FakeDrive:
    """Lists folders by Synology reference: a mount path or a permanent link."""

    def __init__(self, tree: dict[str, list[dict]]) -> None:
        self._tree = tree

    async def list_folder(
        self, ref: object, *, offset: int = 0, limit: int | None = None
    ) -> tuple[list[dict], int]:
        if isinstance(ref, SynologyPath):
            key = ref.path
        elif isinstance(ref, SynologyPermanentLink):
            key = ref.permanent_link
        else:
            raise AssertionError(f"unexpected ref {ref!r}")
        items = self._tree[key][offset:]
        return items, len(self._tree[key])


# /vol/a holds a new chain A/B/c.txt; file IDs differ from permanent links.
_TREE = {
    "/vol/a": [_item("fa", "A", "A", is_dir=True)],
    "A": [_item("fb", "B", "B", is_dir=True)],
    "B": [_item("fc", "C", "c.txt", is_dir=False)],
}


async def _noop_enrich(_self: object, record: NodeRecord) -> NodeRecord:
    return record


async def _slow_drain(q: WriteQueue) -> None:
    """Apply writes late, so a pass that needs them to have landed is caught."""
    while True:
        job = await q.get()
        try:
            await asyncio.sleep(0.05)
            await job()
        finally:
            q.task_done()


class TestNewFolders(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(os.unlink, db_path)
        pool = ThreadPoolExecutor(2)
        self.addCleanup(pool.shutdown)
        off_main = OffMainService(pool=pool)
        self.mirror = MirrorStore(
            db_path, off_main=off_main, schemas={"synology": SYNOLOGY_SCHEMA}
        )
        await self.mirror.ensure_schema()
        await self.mirror.bulk_upsert_nodes(
            [_dir(SERVER_ROOT_ID, "", None), _dir(mount_id("a"), "a", SERVER_ROOT_ID)]
        )
        self.store = SynologyStore(
            mirror=self.mirror, ns=self.mirror.namespace("synology")
        )
        self.syno_paths = SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=make_mounts({"a": "/vol/a"}), root_ids={}),
            store=self.store,
        )
        q = create_write_queue()
        mq: asyncio.Queue = asyncio.Queue()
        self.node_sync = NodeSyncService(
            mirror=self.mirror,
            write_queue=q,
            off_main=off_main,
            mounts={},
            metadata_queue=mq,
        ).for_backend("synology")
        for task in (
            asyncio.create_task(_slow_drain(q)),
            asyncio.create_task(
                metadata_worker(mq, self.node_sync.process_metadata_item)
            ),
        ):
            self.addAsyncCleanup(self._stop, task)
        patcher = patch.object(NodeSyncService, "enrich", new=_noop_enrich)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _stop(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _assert_chain_mirrored(self) -> None:
        nodes = await self.mirror.get_nodes_by_ids(["A", "B", "C"])
        self.assertEqual(set(nodes), {"A", "B", "C"})
        self.assertEqual(nodes["B"].parent_id, "A")
        self.assertEqual(nodes["C"].parent_id, "B")

    async def test_first_scan_lists_children_of_new_folders(self) -> None:
        # given
        svc = StartupScanService(
            drive_api=_FakeDrive(_TREE),  # type: ignore[arg-type]
            mirror=self.mirror,
            store=self.store,
            syno_paths=self.syno_paths,
            node_sync=self.node_sync,
        )

        # when
        await svc.run_initial_scan()
        await self.node_sync.wait_enrichment_drained()

        # then
        await self._assert_chain_mirrored()

    async def test_backfill_lists_children_of_added_folders(self) -> None:
        # given
        svc = BackfillService(
            drive_api=_FakeDrive(_TREE),  # type: ignore[arg-type]
            mirror=self.mirror,
            store=self.store,
            syno_paths=self.syno_paths,
            node_sync=self.node_sync,
        )

        # when
        result = await svc._reconcile_subtree(mount_id("a"))
        await self.node_sync.wait_enrichment_drained()

        # then
        self.assertEqual(result["list_errors"], 0)
        await self._assert_chain_mirrored()

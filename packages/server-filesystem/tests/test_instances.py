"""Two filesystem instances over one host directory keep their nodes apart."""

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.filesystem._blocking import Blocking
from wcpan.drive.server.filesystem._paths import NodePaths
from wcpan.drive.server.filesystem._scan import FilesystemScanner
from wcpan.drive.server.filesystem._store import FILESYSTEM_SCHEMA, FilesystemStore
from wcpan.drive.server.services.ids import NodeIds
from wcpan.drive.server.services.mounts import ensure_mount_nodes

from ._harness import ScanTestCase, make_mounts


class TestTwoInstances(ScanTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # A second instance serving the same directory under another mount.
        ns = await self.mirror.register_schema("fs2", FILESYSTEM_SCHEMA)
        self.mounts2 = make_mounts({"copy": str(self.source)})
        self.node_sync2 = self.core_sync.for_backend("fs2")
        self.scanner2 = FilesystemScanner(
            mirror=self.mirror,
            store=FilesystemStore(mirror=self.mirror, ns=ns),
            node_sync=self.node_sync2,
            ids=NodeIds(mirror=self.mirror, key=ns.key),
            paths=NodePaths(mirror=self.mirror, mounts=self.mounts2),
            blocking=Blocking(executor=self.io_pool),
        )
        await ensure_mount_nodes(
            self.mounts2, mirror=self.mirror, node_sync=self.core_sync
        )

    async def scan_both(self) -> None:
        await self.scan()
        await self.scanner2.scan()
        await self.node_sync2.wait_enrichment_drained()

    async def copy(self, relative: str) -> NodeRecord | None:
        parent = self.mounts2["copy"].id
        record: NodeRecord | None = None
        for name in relative.split("/"):
            children = await self.mirror.get_children(parent)
            record = next((c for c in children if c.name == name), None)
            if record is None:
                return None
            parent = record.id
        return record

    async def copy_id(self, relative: str) -> NodeId:
        record = await self.copy(relative)
        assert record is not None, relative
        return record.id

    async def test_each_instance_mints_and_owns_its_nodes(self) -> None:
        # given
        self.write("a/f.txt", b"x")

        # when
        await self.scan_both()

        # then
        first = await self.node_id("a/f.txt")
        second = await self.copy_id("a/f.txt")
        self.assertNotEqual(first, second)
        self.assertEqual(await self.mirror.owner_of(first), "fs")
        self.assertEqual(await self.mirror.owner_of(second), "fs2")

    async def test_moves_match_only_the_instances_own_entries(self) -> None:
        # given: one object, seen by both instances
        self.write("a/f.txt", b"x")
        await self.scan_both()
        first = await self.node_id("a/f.txt")
        second = await self.copy_id("a/f.txt")

        # when: it moves, and both instances rescan
        (self.source / "b").mkdir()
        (self.source / "a/f.txt").rename(self.source / "b/f.txt")
        await self.scan_both()

        # then: each follows its own node, and neither takes the other's
        self.assertEqual(await self.node_id("b/f.txt"), first)
        self.assertEqual(await self.copy_id("b/f.txt"), second)
        self.assertIsNone(await self.node("a/f.txt"))
        self.assertIsNone(await self.copy("a/f.txt"))

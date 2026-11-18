"""The public harness backends use in their tests."""

import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.testing import ROOT_ID, backend_harness, mount_id
from wcpan.drive.server.types import NodeObservation

from ._fake_backend import FakeSchema


def _file(name: str, parent: str) -> NodeRecord:
    return NodeRecord(
        id=name,
        parent_id=parent,
        name=name,
        is_directory=False,
        created_time=1,
        modified_time=1,
        changed_time=1,
        mime_type="text/plain",
        hash="abc",
        hash_algorithm="md4",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
    )


class BackendHarnessTest(IsolatedAsyncioTestCase):
    async def test_instance_gets_its_mounts_under_the_root(self) -> None:
        async with backend_harness() as server:
            # when
            instance = await server.add_instance("a", FakeSchema(), {"docs": "/d"})
            # then
            record = await server.mirror.get_node_by_id(mount_id("docs"))
            assert record is not None
            self.assertEqual(record.parent_id, ROOT_ID)
            self.assertEqual(instance.context.mounts["docs"].source, "/d")
            self.assertIs(instance.context.core, instance.core)
            self.assertFalse(instance.context.offline)

    async def test_core_services_do_not_expose_the_sync_service(self) -> None:
        async with backend_harness() as server:
            # when
            instance = await server.add_instance("a", FakeSchema(), {"docs": "/d"})
            # then
            self.assertFalse(hasattr(instance.core, "node_sync"))

    async def test_instances_share_one_mirror(self) -> None:
        async with backend_harness() as server:
            # given
            first = await server.add_instance("a", FakeSchema(), {"one": "/1"})
            second = await server.add_instance("b", FakeSchema(), {"two": "/2"})
            self.assertNotEqual(first.namespace.key, second.namespace.key)
            # when
            async with second.core.observe() as changes:
                record = _file("f", mount_id("two"))
                await changes.upserted(NodeObservation(record=record, locator=None))
                await changes.landed()
            # then
            self.assertEqual(await server.mirror.owner_of("f"), "b")

    async def test_held_writes_land_after_release(self) -> None:
        async with backend_harness() as server:
            # given
            instance = await server.add_instance("a", FakeSchema(), {"docs": "/d"})
            gate = await server.hold_writes()
            async with instance.core.observe() as changes:
                record = _file("f", mount_id("docs"))
                await changes.upserted(NodeObservation(record=record, locator=None))
            visible_while_held = await server.mirror.get_node_by_id("f")
            # when
            gate.release()
            async with asyncio.timeout(5):
                await instance.core.writes_landed()
            # then
            self.assertIsNone(visible_while_held)
            self.assertIsNotNone(await server.mirror.get_node_by_id("f"))

    async def test_database_is_removed_on_exit(self) -> None:
        # when
        async with backend_harness() as server:
            path: Path = server.database_path
            self.assertTrue(path.exists())
        # then
        self.assertFalse(path.exists())

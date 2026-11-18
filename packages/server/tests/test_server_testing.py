"""The public harness backends use in their tests."""

import asyncio
import logging
import sqlite3
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.testing import ROOT_ID, backend_harness, mount_id
from wcpan.drive.server.types import NodeObservation

from ._fake_backend import FakeSchema


logging.getLogger("wcpan.drive.server").setLevel(logging.CRITICAL + 1)


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


def _invalid_sql(con: sqlite3.Connection) -> None:
    con.execute("NOT SQL")


class WorkerFailureTest(IsolatedAsyncioTestCase):
    async def test_failed_write_interrupts_a_waiting_block(self) -> None:
        # given
        async def run() -> None:
            async with backend_harness() as server:
                instance = await server.add_instance("a", FakeSchema(), {})
                await instance.core.enqueue_write(_invalid_sql)
                await asyncio.Event().wait()

        task = asyncio.create_task(run())
        self.addCleanup(task.cancel)
        # when
        done, _ = await asyncio.wait({task}, timeout=5)
        # then
        self.assertEqual(done, {task})
        with self.assertRaisesRegex(sqlite3.OperationalError, "NOT"):
            await task

    async def test_failed_write_is_raised_over_the_error_it_caused(self) -> None:
        # when, then
        with self.assertRaisesRegex(sqlite3.OperationalError, "NOT"):
            async with backend_harness() as server:
                instance = await server.add_instance("a", FakeSchema(), {})
                await instance.core.enqueue_write(_invalid_sql)
                await instance.core.writes_landed()

    async def test_failure_is_raised_when_exit_runs_in_another_task(self) -> None:
        # given: entered and left in different tasks, as test fixtures do
        harness = backend_harness()

        async def enter() -> None:
            server = await harness.__aenter__()
            instance = await server.add_instance("a", FakeSchema(), {})
            await instance.core.enqueue_write(_invalid_sql)

        await asyncio.create_task(enter())
        await asyncio.sleep(0.1)
        # when, then
        with self.assertRaisesRegex(sqlite3.OperationalError, "NOT"):
            await asyncio.create_task(harness.__aexit__(None, None, None))

    async def test_outer_cancellation_is_not_swallowed(self) -> None:
        # given
        entered = asyncio.Event()

        async def run() -> None:
            async with backend_harness():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(run())
        await entered.wait()
        # when
        task.cancel()
        # then
        with self.assertRaises(asyncio.CancelledError):
            await task

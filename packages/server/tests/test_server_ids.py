"""Public node IDs minted by the core for each backend instance."""

import asyncio
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.services.ids import NodeIds, UnknownNodeError
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.workers import (
    create_write_queue,
    write_worker,
)


class TestNodeIds(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.pool = ThreadPoolExecutor(4)
        self.mirror = MirrorStore(self.db_path, off_main=OffMainService(pool=self.pool))
        await self.mirror.ensure_schema()
        self.write_queue = create_write_queue()
        self.worker = asyncio.create_task(write_worker(self.write_queue))
        self.a = NodeIds(
            mirror=self.mirror,
            write_queue=self.write_queue,
            key=await self.mirror.register_instance("a"),
        )
        self.b = NodeIds(
            mirror=self.mirror,
            write_queue=self.write_queue,
            key=await self.mirror.register_instance("b"),
        )

    async def asyncTearDown(self) -> None:
        self.worker.cancel()
        with suppress(asyncio.CancelledError):
            await self.worker
        self.pool.shutdown(wait=False, cancel_futures=True)
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                Path(self.db_path + suffix).unlink()

    def _fresh(self, ids: NodeIds) -> NodeIds:
        """Another map of the same instance, without the in-memory cache."""
        return NodeIds(
            mirror=self.mirror,
            write_queue=self.write_queue,
            key=ids._key,  # pyright: ignore[reportPrivateUsage]
        )

    async def test_public_ids_are_stable_and_opaque(self) -> None:
        # when
        first = await self.a.public(["x", "y", "x"])
        again = await self._fresh(self.a).public(["y", "x"])

        # then
        self.assertEqual(first[0], first[2])
        self.assertNotEqual(first[0], first[1])
        self.assertEqual(again, [first[1], first[0]])
        self.assertNotIn("x", first)

    async def test_one_native_id_in_two_instances_gets_two_ids(self) -> None:
        # when
        (in_a,) = await self.a.public(["same"])
        (in_b,) = await self.b.public(["same"])

        # then
        self.assertNotEqual(in_a, in_b)
        self.assertEqual(await self.mirror.owner_of(in_a), "a")
        self.assertEqual(await self.mirror.owner_of(in_b), "b")

    async def test_concurrent_requests_for_a_new_native_id_get_one_id(self) -> None:
        # given: separate caches, so every request reaches the database
        maps = [self._fresh(self.a) for _ in range(8)]

        # when
        results = await asyncio.gather(*(m.public(["new"]) for m in maps))

        # then
        self.assertEqual(len({r[0] for r in results}), 1)

    async def test_mints_run_on_the_write_worker(self) -> None:
        # given: the write worker busy with a long job
        started = threading.Event()
        release = threading.Event()

        def hold(con: sqlite3.Connection) -> None:
            started.set()
            release.wait()

        async def long_job() -> None:
            await self.mirror.write(hold)

        await self.write_queue.put(long_job)
        await asyncio.to_thread(started.wait)

        # when: scan workers mint meanwhile
        maps = [self._fresh(self.a) for _ in range(3)]
        mints = [asyncio.create_task(m.public([f"n{i}"])) for i, m in enumerate(maps)]
        async with asyncio.timeout(5):
            while self.write_queue.qsize() < len(mints):
                await asyncio.sleep(0.01)

        # then: they wait in the queue, not on SQLite's writer lock
        self.assertTrue(all(not t.done() for t in mints))
        release.set()
        self.assertEqual(len({(await t)[0] for t in mints}), 3)

    async def test_lookup_does_not_mint(self) -> None:
        # when / then
        self.assertIsNone(await self.a.lookup("unseen"))
        self.assertIsNone(await self._fresh(self.a).lookup("unseen"))

        # given
        (node_id,) = await self.a.public(["seen"])
        # then
        self.assertEqual(await self._fresh(self.a).lookup("seen"), node_id)
        self.assertIsNone(await self.b.lookup("seen"))

    async def test_native_of(self) -> None:
        # given
        (node_id,) = await self.a.public(["native"])

        # then
        self.assertEqual(await self._fresh(self.a).native_of(node_id), "native")
        with self.assertRaises(UnknownNodeError):
            await self.b.native_of(node_id)
        with self.assertRaises(UnknownNodeError):
            await self.a.native_of(NodeId("unknown"))

    async def test_minted_ids_are_unique_and_unmapped_until_observed(self) -> None:
        # when
        minted = {self.a.mint() for _ in range(100)}

        # then
        self.assertEqual(len(minted), 100)
        con = sqlite3.connect(self.db_path)
        try:
            (count,) = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()
        finally:
            con.close()
        self.assertEqual(count, 0)

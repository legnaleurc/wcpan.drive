from asyncio import TaskGroup
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Manager, get_context
from sqlite3 import Cursor
from tempfile import NamedTemporaryFile
from threading import Event
from unittest import IsolatedAsyncioTestCase, skip

from wcpan.drive.sqlite._lib import OffMainProcess, read_only, read_write


class TransactionTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._manager = self.enterContext(Manager())
        file = self.enterContext(NamedTemporaryFile())
        self._dsn = file.name
        context = get_context("spawn")
        self._pool = self.enterContext(ProcessPoolExecutor(mp_context=context))
        self._bg = OffMainProcess(dsn=self._dsn, pool=self._pool)
        with read_write(self._dsn) as query:
            _prepare(query)

    def testRead(self):
        with read_only(self._dsn) as query:
            rv = _inner_select(query, "alice")

        self.assertIsNotNone(rv)
        assert rv
        self.assertEqual(rv, 1)

    def testWrite(self):
        with read_write(self._dsn) as query:
            _inner_insert(query, 2, "bob")

        with read_only(self._dsn) as query:
            rv = _inner_select(query, "bob")

        self.assertIsNotNone(rv)
        assert rv
        self.assertEqual(rv, 2)

    async def testParallelRead(self):
        event = self._manager.Event()
        async with TaskGroup() as group:
            r1 = group.create_task(self._bg(_sync_select, "alice", event))
            r2 = group.create_task(self._bg(_sync_select, "alice", event))
            event.set()

        rv = r1.result()
        assert rv is not None
        self.assertEqual(rv, 1)
        rv = r2.result()
        assert rv is not None
        self.assertEqual(rv, 1)

    @skip("unstable")
    async def testParallelWrite(self):
        event = self._manager.Event()
        with self.assertRaises(Exception, msg="database is locked"):
            async with TaskGroup() as group:
                group.create_task(self._bg(_sync_update, 1, "bob", event))
                group.create_task(self._bg(_sync_update, 1, "cat", event))
                event.set()

    @skip("unstable")
    async def testParallelReadWrite(self):
        event = self._manager.Event()
        async with TaskGroup() as group:
            group.create_task(self._bg(_sync_update, 1, "cat", event))
            r = group.create_task(self._bg(_sync_select, "alice", event))
            event.set()

        rv = r.result()
        assert rv is not None
        self.assertEqual(rv, 1)


def _prepare(query: Cursor):
    query.execute(
        """
        CREATE TABLE student (
            id INTEGER NOT NULL,
            name VARCHAR(64),
            PRIMARY KEY (id)
        );
        """
    )
    query.execute(
        """
        INSERT INTO student
        (id, name)
        VALUES
        (?, ?);
        """,
        (1, "alice"),
    )


def _inner_select(query: Cursor, name: str) -> int | None:
    query.execute(
        """
        SELECT id FROM student WHERE name=?;
        """,
        (name,),
    )
    rv = query.fetchone()
    if rv is None:
        return None
    return rv["id"]


def _sync_select(dsn: str, name: str, event: Event) -> int | None:
    with read_only(dsn, timeout=0) as query:
        rv = _inner_select(query, name)
        event.wait()
        return rv


def _inner_insert(query: Cursor, id: int, name: str) -> None:
    query.execute(
        """
        INSERT INTO student
        (id, name)
        VALUES
        (?, ?);
        """,
        (id, name),
    )


def _inner_update(query: Cursor, id: int, name: str) -> None:
    query.execute(
        """
        UPDATE student
        SET name = ?
        WHERE id = ?
        ;
        """,
        (name, id),
    )


def _sync_update(dsn: str, id: int, name: str, event: Event) -> None:
    with read_write(dsn, timeout=0) as query:
        _inner_update(query, id, name)
        event.wait()

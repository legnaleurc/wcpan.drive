import asyncio
import contextlib as cl
import functools as ft
import os
import sqlite3
import tempfile
import unittest as ut
import unittest.mock as utm

from wcpan.drive.cache import (
    ReadOnly,
    ReadWrite,
    Cache,
    CacheError,
    node_from_dict,
)
from wcpan.drive.util import create_executor

from .util import create_root, create_folder, create_file


class TestTransaction(ut.TestCase):

    def setUp(self):
        _, self._file = tempfile.mkstemp()
        with connect(self._file) as db:
            prepare(db)

    def tearDown(self):
        os.unlink(self._file)

    def testRead(self):
        with connect(self._file) as db:
            with ReadOnly(db) as query:
                inner_select(query)
                rv = query.fetchone()

        self.assertIsNotNone(rv)
        self.assertEqual(rv['id'], 1)

    def testWrite(self):
        with connect(self._file) as db:
            with ReadWrite(db) as query:
                inner_insert(query)

            with cl.closing(db.cursor()) as query:
                query.execute('''
                    SELECT id FROM student WHERE name=?;
                ''', ('bob',))
                rv = query.fetchone()

        self.assertIsNotNone(rv)
        self.assertEqual(rv['id'], 2)

    def testParallelReading(self):
        with connect(self._file) as db1, \
             connect(self._file) as db2:
            with ReadOnly(db1) as q1:
                inner_select(q1)
                with ReadOnly(db2) as q2:
                    inner_select(q2)

    def testWriteWhileReading(self):
        with connect(self._file) as rdb, \
             connect(self._file) as wdb:
            with self.assertRaises(sqlite3.OperationalError) as e:
                with ReadOnly(rdb) as rq:
                    inner_select(rq)
                    with ReadWrite(wdb) as wq:
                        inner_insert(wq)

        self.assertEqual(str(e.exception), 'database is locked')

    def testReadWhileWriting(self):
        with connect(self._file) as rdb, \
             connect(self._file) as wdb:
            with ReadWrite(wdb) as wq:
                inner_insert(wq)
                with ReadOnly(rdb) as rq:
                    rq.execute('''
                        SELECT id FROM student WHERE name=?;
                    ''', ('bob',))
                    rv = rq.fetchone()

        self.assertIsNone(rv)

    def testParallelWriting(self):
        with connect(self._file) as db1, \
             connect(self._file) as db2:
            with self.assertRaises(sqlite3.OperationalError) as e:
                with ReadWrite(db1) as q1:
                    inner_insert(q1)
                    with ReadWrite(db2) as q2:
                        inner_insert(q2)

        self.assertEqual(str(e.exception), 'database is locked')


class TestNodeCache(ut.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _, self._file = tempfile.mkstemp()

        async with cl.AsyncExitStack() as ctx:
            pool = ctx.enter_context(create_executor())
            self._db = await ctx.enter_async_context(Cache(self._file, pool))
            self._stack = ctx.pop_all()

        await initialize_nodes(self._db)

    async def asyncTearDown(self):
        await self._stack.aclose()
        os.unlink(self._file)

    async def testRoot(self):
        node = await self._db.get_root_node()
        self.assertEqual(node.id_, '__ROOT_ID__')

    async def testSearch(self):
        nodes = await self._db.find_nodes_by_regex(r'^f1$')
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.id_, '__F1_ID__')
        path = await self._db.get_path_by_id(node.id_)
        self.assertEqual(path, '/d1/f1')

    async def testGetInvalidPath(self):
        with self.assertRaises(CacheError):
            await self._db.get_path_by_id('__INVALID_ID__')


def connect(path):
    db = sqlite3.connect(path, timeout=0.1)
    db.row_factory = sqlite3.Row
    return db


def prepare(db):
    with cl.closing(db.cursor()) as query:
        query.execute('''
            CREATE TABLE student (
                id INTEGER NOT NULL,
                name VARCHAR(64),
                PRIMARY KEY (id)
            );
        ''')
        query.execute('''
            INSERT INTO student
            (id, name)
            VALUES
            (?, ?);
        ''', (1, 'alice'))


def inner_select(query):
    query.execute('''
        SELECT id FROM student WHERE name=?;
    ''', ('alice',))


def inner_insert(query):
    query.execute('''
        INSERT INTO student
        (id, name)
        VALUES
        (?, ?);
    ''', (2, 'bob'))


async def initialize_nodes(db):
    data = create_root()
    node = node_from_dict(data)
    await db.insert_node(node)

    data = [
        {
            'removed': False,
            'node': create_folder('__D1_ID__', 'd1', '__ROOT_ID__'),
        },
        {
            'removed': False,
            'node': create_folder('__D2_ID__', 'd2', '__ROOT_ID__'),
        },
        {
            'removed': False,
            'node': create_file(
                '__F1_ID__',
                'f1',
                '__D1_ID__',
                1337,
                '__F1_MD5__',
                'text/plain',
            ),
        },
        {
            'removed': False,
            'node': create_file(
                '__F2_ID__',
                'f2',
                '__D2_ID__',
                1234,
                '__F2_MD5__',
                'text/plain',
            ),
        },
    ]
    await db.apply_changes(data, '2')

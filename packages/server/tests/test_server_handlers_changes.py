"""Tests for change feed handlers: get_cursor, get_root, get_changes."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wcpan.drive.protocol.lib import node_record_from_dict
from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.handlers.changes import (
    get_changes,
    get_cursor,
    get_root,
)
from wcpan.drive.server.keys import (
    CHANGE_SERVICE_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    STORAGE_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.services.paths import SERVER_ROOT_ID
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import create_write_queue


_EPOCH = 0


def _make_node(node_id: str = "n1", parent_id: str | None = "p1") -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name="x",
        is_directory=False,
        created_time=_EPOCH,
        modified_time=_EPOCH,
        changed_time=_EPOCH,
        mime_type="text/plain",
        hash="abc",
        hash_algorithm="md4",
        size=10,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class _FakeOffMain:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def untimed(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeStorage:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._cursor: int = 42
        self._changes: list[tuple[str, bool, NodeRecord | None]] = []

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def get_cursor(self) -> int:
        return self._cursor

    async def get_changes_since(
        self, cursor: int, max_size: int
    ) -> tuple[list[tuple[str, bool, NodeRecord | None]], int, bool]:
        return self._changes[:max_size], self._cursor, len(self._changes) > max_size

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> set[str]:
        for o in observations:
            self._nodes[o.record.id] = o.record
        return {o.record.id for o in observations}


def _make_app(storage: _FakeStorage) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    app[READY_KEY] = True
    app[STORAGE_KEY] = storage
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq
    app[CHANGE_SERVICE_KEY] = NodeSyncService(
        storage=storage,
        write_queue=wq,
        off_main=off_main,
        mounts={},
        metadata_queue=asyncio.Queue(),
    )  # type: ignore[arg-type]

    app.router.add_get("/api/v1/cursor", get_cursor)
    app.router.add_get("/api/v1/root", get_root)
    app.router.add_get("/api/v1/changes", get_changes)
    return app


# ---------------------------------------------------------------------------
# get_cursor
# ---------------------------------------------------------------------------


class TestGetCursor(IsolatedAsyncioTestCase):
    async def test_returns_cursor(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cursor")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["cursor"], 42)


# ---------------------------------------------------------------------------
# get_root
# ---------------------------------------------------------------------------


class TestGetRoot(IsolatedAsyncioTestCase):
    async def test_returns_root(self):
        storage = _FakeStorage()
        root = NodeRecord(
            id=SERVER_ROOT_ID,
            parent_id=None,
            name="root",
            is_directory=True,
            created_time=_EPOCH,
            modified_time=_EPOCH,
            changed_time=_EPOCH,
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
        storage._nodes[SERVER_ROOT_ID] = root
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/root")
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, SERVER_ROOT_ID)

    async def test_not_found(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/root")
            self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# get_changes
# ---------------------------------------------------------------------------


class TestGetChanges(IsolatedAsyncioTestCase):
    async def test_returns_changes(self):
        storage = _FakeStorage()
        node = _make_node("n1")
        storage._changes = [("n1", False, node)]
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/changes", params={"cursor": "0"})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["cursor"], 42)
        self.assertFalse(body["has_more"])
        self.assertEqual(len(body["changes"]), 1)
        self.assertFalse(body["changes"][0]["removed"])
        self.assertIn("node", body["changes"][0])

    async def test_returns_removals(self):
        storage = _FakeStorage()
        storage._changes = [("del1", True, None)]
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/changes", params={"cursor": "0"})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(len(body["changes"]), 1)
        self.assertTrue(body["changes"][0]["removed"])
        self.assertEqual(body["changes"][0]["node_id"], "del1")

    async def test_invalid_cursor_returns_400(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/changes", params={"cursor": "abc"})
            self.assertEqual(resp.status, 400)

    async def test_invalid_max_size_returns_400(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/changes", params={"cursor": "0", "max_size": "not-a-number"}
            )
            self.assertEqual(resp.status, 400)

    async def test_default_params(self):
        storage = _FakeStorage()
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/changes")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["changes"], [])

    async def test_max_size_caps_at_1000(self):
        storage = _FakeStorage()
        storage._changes = [("n1", False, _make_node())] * 5
        app = _make_app(storage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/changes", params={"cursor": "0", "max_size": "9999"}
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        # max_size is capped at 1000 internally, and we only have 5 items
        self.assertEqual(len(body["changes"]), 5)

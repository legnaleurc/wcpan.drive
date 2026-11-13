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
    MIRROR_KEY,
    OFF_MAIN_KEY,
    READY_KEY,
    WRITE_QUEUE_KEY,
)
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import create_write_queue

from ._mounts import ROOT_ID


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


class _FakeMirror:
    def __init__(self) -> None:
        self.epoch = "e1"
        self.since: int | None = None
        self._nodes: dict[str, NodeRecord] = {}
        self._cursor: int = 42
        self._changes: list[tuple[str, bool, NodeRecord | None]] = []

    async def get_node_by_id(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    async def get_root(self) -> NodeRecord | None:
        return next((n for n in self._nodes.values() if n.parent_id is None), None)

    async def get_cursor(self) -> int:
        return self._cursor

    async def get_changes_since(
        self, cursor: int, max_size: int
    ) -> tuple[list[tuple[str, bool, NodeRecord | None]], int, bool]:
        self.since = cursor
        return self._changes[:max_size], self._cursor, len(self._changes) > max_size

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        self._nodes[record.id] = record

    async def apply_observations(self, observations: list[NodeObservation]) -> set[str]:
        for o in observations:
            self._nodes[o.record.id] = o.record
        return {o.record.id for o in observations}


def _make_app(mirror: _FakeMirror) -> web.Application:
    app = web.Application()
    off_main = _FakeOffMain()
    wq = create_write_queue()
    app[READY_KEY] = True
    app[MIRROR_KEY] = mirror
    app[OFF_MAIN_KEY] = off_main
    app[WRITE_QUEUE_KEY] = wq

    app.router.add_get("/api/v1/cursor", get_cursor)
    app.router.add_get("/api/v1/root", get_root)
    app.router.add_get("/api/v1/changes", get_changes)
    return app


# ---------------------------------------------------------------------------
# get_cursor
# ---------------------------------------------------------------------------


class TestGetCursor(IsolatedAsyncioTestCase):
    async def test_returns_cursor(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/cursor")
            # then
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["cursor"], "e1.42")


# ---------------------------------------------------------------------------
# get_root
# ---------------------------------------------------------------------------


class TestGetRoot(IsolatedAsyncioTestCase):
    async def test_returns_root(self):
        # given
        mirror = _FakeMirror()
        root = NodeRecord(
            id=ROOT_ID,
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
        mirror._nodes[ROOT_ID] = root
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/root")
            # then
            self.assertEqual(resp.status, 200)
            record = node_record_from_dict(await resp.json())
        self.assertEqual(record.id, ROOT_ID)

    async def test_not_found(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/root")
            # then
            self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# get_changes
# ---------------------------------------------------------------------------


class TestGetChanges(IsolatedAsyncioTestCase):
    async def test_returns_changes(self):
        # given
        mirror = _FakeMirror()
        node = _make_node("n1")
        mirror._changes = [("n1", False, node)]
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes", params={"cursor": "0"})
            # then
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["cursor"], "e1.42")
        self.assertFalse(body["has_more"])
        self.assertEqual(len(body["changes"]), 1)
        self.assertFalse(body["changes"][0]["removed"])
        self.assertIn("node", body["changes"][0])

    async def test_returns_removals(self):
        # given
        mirror = _FakeMirror()
        mirror._changes = [("del1", True, None)]
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes", params={"cursor": "0"})
            # then
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(len(body["changes"]), 1)
        self.assertTrue(body["changes"][0]["removed"])
        self.assertEqual(body["changes"][0]["node_id"], "del1")

    async def test_invalid_cursor_returns_400(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes", params={"cursor": "abc"})
            # then
            self.assertEqual(resp.status, 400)

    async def test_malformed_cursor_returns_400(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            for cursor in ("e1.", ".5", "e1.x", "e1.-1"):
                with self.subTest(cursor=cursor):
                    # when
                    resp = await client.get(
                        "/api/v1/changes", params={"cursor": cursor}
                    )
                    # then
                    self.assertEqual(resp.status, 400)

    async def test_cursor_of_current_epoch_resumes(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes", params={"cursor": "e1.7"})
            # then
            self.assertEqual(resp.status, 200)
        self.assertEqual(mirror.since, 7)

    async def test_bootstrap_cursor_starts_from_beginning(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes", params={"cursor": "0"})
            # then
            self.assertEqual(resp.status, 200)
        self.assertEqual(mirror.since, 0)

    async def test_stale_cursor_returns_410(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        cases = {
            "other epoch": "e0.7",
            "legacy integer": "7",
            "past the end (restored database)": "e1.43",
        }
        async with TestClient(TestServer(app)) as client:
            for label, cursor in cases.items():
                with self.subTest(label):
                    # when
                    resp = await client.get(
                        "/api/v1/changes", params={"cursor": cursor}
                    )
                    # then
                    self.assertEqual(resp.status, 410)
        self.assertIsNone(mirror.since)

    async def test_invalid_max_size_returns_400(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get(
                "/api/v1/changes", params={"cursor": "0", "max_size": "not-a-number"}
            )
            # then
            self.assertEqual(resp.status, 400)

    async def test_default_params(self):
        # given
        mirror = _FakeMirror()
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get("/api/v1/changes")
            # then
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["changes"], [])

    async def test_max_size_caps_at_1000(self):
        # given
        mirror = _FakeMirror()
        mirror._changes = [("n1", False, _make_node())] * 5
        app = _make_app(mirror)
        async with TestClient(TestServer(app)) as client:
            # when
            resp = await client.get(
                "/api/v1/changes", params={"cursor": "0", "max_size": "9999"}
            )
            # then
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        # max_size is capped at 1000 internally, and we only have 5 items
        self.assertEqual(len(body["changes"]), 5)

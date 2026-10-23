"""Tests for ClientFileService helpers."""

from datetime import UTC, datetime
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase

from wcpan.drive.client.exceptions import NodeExistsError, NodeNotFoundError
from wcpan.drive.client.remote.exceptions import (
    SynologyAccessError,
    SynologyServerError,
)
from wcpan.drive.client.remote.service import ClientFileService, _check
from wcpan.drive.client.types import Node


class TestCheck(TestCase):
    def test_404_raises_node_not_found(self):
        # given
        status = 404
        # when / then
        with self.assertRaises(NodeNotFoundError):
            _check(status, "get_root")

    def test_access_statuses_raise_access_error(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                # when
                with self.assertRaises(SynologyAccessError) as ctx:
                    _check(status, "delete")
                # then
                self.assertEqual(ctx.exception.status, status)

    def test_500_raises_server_error_with_status(self):
        # given
        status = 500
        # when
        with self.assertRaises(SynologyServerError) as ctx:
            _check(status, "move")
        # then
        self.assertEqual(ctx.exception.status, 500)

    def test_400_raises_server_error(self):
        # given
        status = 400
        # when
        with self.assertRaises(SynologyServerError) as ctx:
            _check(status, "delete")
        # then
        self.assertEqual(ctx.exception.status, 400)

    def test_2xx_ok(self):
        # given
        for status in (200, 201, 204):
            with self.subTest(status=status):
                # when / then — no exception
                _check(status, "op")


def _parent_node() -> Node:
    t = datetime.now(UTC)
    return Node(
        id="p",
        parent_id=None,
        name="root",
        is_directory=True,
        is_trashed=False,
        created_time=t,
        modified_time=t,
        changed_time=t,
        mime_type="application/x-directory",
        hash="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )


class _Fake409Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self) -> "_Fake409Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class TestCreateDirectory409(IsolatedAsyncioTestCase):
    def _node_body(self) -> dict:
        t = 1_704_067_200
        return {
            "id": "existing-dir",
            "mutable_id": "existing-dir",
            "parent_id": "p",
            "name": "d",
            "is_directory": True,
            "created_time": t,
            "modified_time": t,
            "changed_time": t,
            "mime_type": "application/x-directory",
            "hash": "",
            "size": 0,
            "is_image": False,
            "is_video": False,
            "width": 0,
            "height": 0,
            "ms_duration": 0,
        }

    async def test_exist_ok_returns_node(self) -> None:
        body = self._node_body()

        class _Sess:
            def post(self, *a, **k):
                return _Fake409Response(409, body)

        svc = ClientFileService(session=_Sess(), server_url="http://srv")  # type: ignore[arg-type]
        node = await svc.create_directory(
            "d", _parent_node(), exist_ok=True, private=None
        )
        self.assertEqual(node.id, "existing-dir")

    async def test_exist_ok_false_raises_node_exists(self) -> None:
        body = self._node_body()

        class _Sess2:
            def post(self, *a, **k):
                return _Fake409Response(409, body)

        svc = ClientFileService(session=_Sess2(), server_url="http://srv")  # type: ignore[arg-type]
        with self.assertRaises(NodeExistsError) as ctx:
            await svc.create_directory(
                "d", _parent_node(), exist_ok=False, private=None
            )
        self.assertEqual(ctx.exception.node.id, "existing-dir")

    async def test_missing_node_body_raises_server_error(self) -> None:
        class _Sess:
            def post(self, *a, **k):
                return _Fake409Response(409, {})

        svc = ClientFileService(session=_Sess(), server_url="http://srv")  # type: ignore[arg-type]
        with self.assertRaises(SynologyServerError) as ctx:
            await svc.create_directory("d", _parent_node(), exist_ok=True, private=None)
        self.assertEqual(ctx.exception.status, 409)


class _FakeChangesResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> "_FakeChangesResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeChangesSession:
    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self._pages = pages

    def get(self, _url: str, *, params: dict[str, str]):
        cursor = params["cursor"]
        return _FakeChangesResponse(self._pages[cursor])


class TestGetChangesReplay(IsolatedAsyncioTestCase):
    def _node_change(self, node_id: str, parent_id: str | None = "p") -> dict[str, Any]:
        t = 1_704_067_200
        return {
            "removed": False,
            "node": {
                "id": node_id,
                "mutable_id": node_id,
                "parent_id": parent_id,
                "name": node_id,
                "is_directory": False,
                "created_time": t,
                "modified_time": t,
                "changed_time": t,
                "mime_type": "text/plain",
                "hash": "",
                "size": 1,
                "is_image": False,
                "is_video": False,
                "width": 0,
                "height": 0,
                "ms_duration": 0,
            },
        }

    def _remove_change(self, node_id: str) -> dict[str, Any]:
        return {"removed": True, "node_id": node_id}

    async def test_replays_duplicate_same_node_across_pages(self) -> None:
        pages = {
            "0": {
                "cursor": 2,
                "has_more": True,
                "changes": [self._node_change("n1"), self._node_change("n1")],
            },
            "2": {
                "cursor": 3,
                "has_more": False,
                "changes": [self._remove_change("n1")],
            },
        }
        svc = ClientFileService(
            session=_FakeChangesSession(pages), server_url="http://srv"
        )  # type: ignore[arg-type]
        actions_by_page = []
        async for actions, cursor in svc.get_changes("0"):
            actions_by_page.append((actions, cursor))

        self.assertEqual([c for _, c in actions_by_page], ["2", "3"])
        self.assertEqual(len(actions_by_page[0][0]), 2)
        self.assertEqual(actions_by_page[0][0][0][1].id, "n1")
        self.assertEqual(actions_by_page[0][0][1][1].id, "n1")
        self.assertEqual(actions_by_page[1][0], [(True, "n1")])

    async def test_parent_child_delete_order_does_not_break_iteration(self) -> None:
        pages = {
            "0": {
                "cursor": 3,
                "has_more": False,
                "changes": [
                    self._remove_change("parent"),
                    self._remove_change("child"),
                    self._node_change("child", "parent"),
                ],
            }
        }
        svc = ClientFileService(
            session=_FakeChangesSession(pages), server_url="http://srv"
        )  # type: ignore[arg-type]

        seen = []
        async for actions, cursor in svc.get_changes("0"):
            seen.append((actions, cursor))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "3")
        self.assertEqual(seen[0][0][0], (True, "parent"))
        self.assertEqual(seen[0][0][1], (True, "child"))
        self.assertFalse(seen[0][0][2][0])
        self.assertEqual(seen[0][0][2][1].parent_id, "parent")

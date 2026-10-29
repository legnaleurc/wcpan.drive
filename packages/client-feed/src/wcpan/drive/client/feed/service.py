"""Backend implementation that talks to the wcpan.drive.server feed server."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast, override

from aiohttp import ClientSession
from wcpan.drive.client.exceptions import NodeExistsError, NodeNotFoundError
from wcpan.drive.client.types import (
    Backend,
    ChangeAction,
    CreateHasher,
    MediaInfo,
    Node,
    PrivateDict,
    ReadableFile,
    WritableFile,
)

from ._lib import (
    ChangeDict,
    NodeRecordDict,
    RemovedChangeDict,
    UpsertChangeDict,
    node_from_record,
    node_record_from_dict,
)
from .exceptions import SynologyAccessError, SynologyServerError
from .hasher import get_hasher_factory
from .http409 import node_from_409
from .readable import ClientReadableFile
from .writable import create_writable


class FeedBackend(Backend):
    def __init__(self, *, session: ClientSession, server_url: str) -> None:
        self._session = session
        self._url = server_url.rstrip("/")

    @property
    @override
    def api_version(self) -> int:
        return 7

    @override
    async def get_initial_cursor(self) -> str:
        return "0"

    @override
    async def get_root(self) -> Node:
        async with self._session.get(f"{self._url}/api/v1/root") as response:
            _check(response.status, "get_root")
            data = await response.json()
        return node_from_record(node_record_from_dict(cast(NodeRecordDict, data)))

    @override
    async def get_changes(
        self,
        cursor: str,
    ) -> AsyncIterator[tuple[list[ChangeAction], str]]:
        has_more = True
        while has_more:
            async with self._session.get(
                f"{self._url}/api/v1/changes",
                params={"cursor": cursor},
            ) as response:
                _check(response.status, "get_changes")
                data = await response.json()

            cursor = str(data["cursor"])
            has_more = bool(data.get("has_more", False))
            raw_changes = cast(list[ChangeDict], data.get("changes", []))

            actions: list[ChangeAction] = []
            for raw in raw_changes:
                if raw["removed"]:
                    removed = cast(RemovedChangeDict, raw)
                    actions.append((True, removed["node_id"]))
                else:
                    upsert = cast(UpsertChangeDict, raw)
                    record = node_record_from_dict(upsert["node"])
                    actions.append((False, node_from_record(record)))

            yield actions, cursor

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None,
        new_name: str | None,
    ) -> Node:
        if not node.parent_id:
            raise ValueError("Cannot move root node")

        body: dict[str, str] = {}
        if new_name:
            body["name"] = new_name
        if new_parent:
            body["parent_id"] = new_parent.id

        async with self._session.patch(
            f"{self._url}/api/v1/nodes/{node.id}",
            json=body,
        ) as response:
            _check(response.status, "move")
            data = await response.json()

        return node_from_record(node_record_from_dict(cast(NodeRecordDict, data)))

    @override
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        async with self._session.delete(
            f"{self._url}/api/v1/nodes/{node.id}"
        ) as response:
            if response.status == 404:
                raise NodeNotFoundError(node.id)
            _check(response.status, "delete")

    @override
    async def restore(self, node: Node) -> Node:
        raise NotImplementedError("restore is not supported")

    @override
    async def purge_trash(self) -> None:
        pass

    @override
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool,
        private: PrivateDict | None,
    ) -> Node:
        async with self._session.post(
            f"{self._url}/api/v1/nodes",
            json={"name": name, "parent_id": parent.id},
        ) as response:
            if response.status == 409:
                node = await node_from_409(response)
                if node is None:
                    raise SynologyServerError(
                        "create_directory conflict: 409 without node body",
                        status=409,
                    )
                if exist_ok:
                    return node
                raise NodeExistsError(node)
            _check(response.status, "create_directory")
            data = await response.json()

        return node_from_record(node_record_from_dict(cast(NodeRecordDict, data)))

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncGenerator[ReadableFile]:
        async with ClientReadableFile(
            node, session=self._session, server_url=self._url
        ) as fin:
            yield fin

    @asynccontextmanager
    @override
    async def upload_file(
        self,
        name: str,
        parent: Node,
        *,
        size: int | None,
        mime_type: str | None,
        media_info: MediaInfo | None,
        private: PrivateDict | None,
    ) -> AsyncGenerator[WritableFile]:
        async with create_writable(
            session=self._session,
            server_url=self._url,
            parent_id=parent.id,
            name=name,
            size=size,
            mime_type=mime_type,
            media_info=media_info,
        ) as fout:
            yield fout

    @override
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        return get_hasher_factory(node)

    @override
    async def is_authenticated(self) -> bool:
        return True  # session managed by context manager

    @override
    async def authenticate(self) -> None:
        pass


def _check(status: int, operation: str) -> None:
    if status == 404:
        raise NodeNotFoundError(operation)
    if status in (401, 403, 429):
        raise SynologyAccessError(f"{operation} failed with status {status}", status)
    if status >= 500:
        raise SynologyServerError(f"{operation} failed", status=status)
    if status >= 400:
        raise SynologyServerError(f"{operation} returned {status}", status=status)

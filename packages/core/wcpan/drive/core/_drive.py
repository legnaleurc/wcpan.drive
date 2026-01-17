from asyncio import Lock
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from itertools import tee
from pathlib import PurePath
from typing import override

from ._lib import (
    else_none,
    is_valid_name,
    normalize_path,
)
from .exceptions import (
    InvalidServiceError,
    NodeExistsError,
    UnauthorizedError,
)
from .types import (
    ChangeAction,
    CreateFileService,
    CreateFileServiceMiddleware,
    CreateHasher,
    CreateService,
    CreateServiceMiddleware,
    CreateSnapshotService,
    CreateSnapshotServiceMiddleware,
    Drive,
    FileService,
    MediaInfo,
    Node,
    ReadableFile,
    Service,
    SnapshotService,
    WritableFile,
)


_API_VERSION = 4


@asynccontextmanager
async def create_drive(
    *,
    file: CreateFileService,
    snapshot: CreateSnapshotService,
    file_middleware: Sequence[CreateFileServiceMiddleware] | None = None,
    snapshot_middleware: Sequence[CreateSnapshotServiceMiddleware] | None = None,
) -> AsyncIterator[Drive]:
    async with (
        _create_service(
            create_service=snapshot,
            middleware_list=snapshot_middleware,
        ) as snapshot_service,
        _create_service(
            create_service=file, middleware_list=file_middleware
        ) as file_service,
    ):
        yield _DefaultDrive(
            file_service=file_service, snapshot_service=snapshot_service
        )


@asynccontextmanager
async def _create_service[T: Service](
    create_service: CreateService[T],
    middleware_list: Sequence[CreateServiceMiddleware[T]] | None,
):
    async with AsyncExitStack() as stack:
        service = await stack.enter_async_context(create_service())
        if service.api_version != _API_VERSION:
            raise InvalidServiceError(
                f"invalid version: required {_API_VERSION}, got {service.api_version}"
            )

        if not middleware_list:
            middleware_list = []

        for create_middleware in middleware_list:
            service = await stack.enter_async_context(create_middleware(service))
            if service.api_version != _API_VERSION:
                raise InvalidServiceError(
                    f"invalid version: required {_API_VERSION}, got {service.api_version}"
                )

        yield service


class _DefaultDrive(Drive):
    def __init__(
        self,
        *,
        file_service: FileService,
        snapshot_service: SnapshotService,
    ) -> None:
        self._sync_lock = Lock()
        self._fs = file_service
        self._ss = snapshot_service

    @override
    async def get_root(self) -> Node:
        return await self._ss.get_root()

    @override
    async def get_node_by_id(self, node_id: str) -> Node:
        return await self._ss.get_node_by_id(node_id)

    @override
    async def get_node_by_path(self, path: PurePath) -> Node:
        path = normalize_path(path)
        return await self._ss.get_node_by_path(path)

    @override
    async def resolve_path(self, node: Node) -> PurePath:
        return await self._ss.resolve_path_by_id(node.id)

    @override
    async def get_child_by_name(
        self,
        name: str,
        parent: Node,
    ) -> Node:
        return await self._ss.get_child_by_name(name, parent.id)

    @override
    async def get_children(self, parent: Node) -> list[Node]:
        return await self._ss.get_children_by_id(parent.id)

    @override
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        rv = await self._ss.get_trashed_nodes()
        if flatten:
            return rv

        ancestor_set = set(_.id for _ in rv if _.is_directory)
        if not ancestor_set:
            return rv

        table = {_.id: _ for _ in rv}
        return [_ for _ in rv if not _in_ancestor_set(table, _, ancestor_set)]

    @override
    async def find_nodes_by_regex(self, pattern: str) -> list[Node]:
        return await self._ss.find_nodes_by_regex(pattern)

    @override
    async def walk(
        self,
        node: Node,
        *,
        include_trashed: bool = False,
    ) -> AsyncIterator[tuple[Node, list[Node], list[Node]]]:
        if not node.is_directory:
            return
        if node.is_trashed and not include_trashed:
            return

        q = deque([node])
        while q:
            node = q.popleft()
            children = await self.get_children(node)
            children = (_ for _ in children if not _.is_trashed or include_trashed)

            directories, files = tee(children, 2)
            directories = [_ for _ in directories if _.is_directory]
            files = [_ for _ in files if not _.is_directory]

            yield node, directories, files

            q.extend(directories)

    @override
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool = False,
    ) -> Node:
        # sanity check
        if not parent.is_directory:
            raise ValueError("parent is not a directory")
        if not name:
            raise ValueError("directory name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in directory name")
        if not await self.is_authorized():
            raise UnauthorizedError()

        if not exist_ok:
            node = await else_none(
                self.get_child_by_name(
                    name,
                    parent,
                )
            )
            if node:
                raise NodeExistsError(node)

        return await self._fs.create_directory(
            name,
            parent,
            exist_ok=exist_ok,
            private=None,
        )

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncIterator[ReadableFile]:
        # sanity check
        if node.is_directory:
            raise ValueError("node should be a file")
        if not await self.is_authorized():
            raise UnauthorizedError()

        async with self._fs.download_file(node) as file:
            yield file

    @asynccontextmanager
    @override
    async def upload_file(
        self,
        name: str,
        parent: Node,
        *,
        size: int | None = None,
        mime_type: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> AsyncIterator[WritableFile]:
        # sanity check
        if not parent.is_directory:
            raise ValueError("parent is not a directory")
        if not name:
            raise ValueError("directory name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in directory name")
        if not await self.is_authorized():
            raise UnauthorizedError()

        node = await else_none(self.get_child_by_name(name, parent))
        if node:
            raise NodeExistsError(node)

        async with self._fs.upload_file(
            name,
            parent,
            size=size,
            mime_type=mime_type,
            media_info=media_info,
            private=None,
        ) as file:
            yield file

    @override
    async def purge_trash(self) -> None:
        # sanity check
        if not await self.is_authorized():
            raise UnauthorizedError()

        await self._fs.purge_trash()

    @override
    async def delete(self, node: Node) -> None:
        if not await self.is_authorized():
            raise UnauthorizedError()

        await self._fs.delete(node)

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
        trashed: bool | None = None,
    ) -> Node:
        # sanity check
        if not await self.is_authorized():
            raise UnauthorizedError()

        if not new_parent and not new_name and trashed is None:
            raise ValueError("nothing to move")

        if new_name and not is_valid_name(new_name):
            raise ValueError("no `/` or `\\` allowed in file name")

        root_node = await self.get_root()
        if node.id == root_node.id:
            raise ValueError("root node is immutable")

        if new_parent:
            if new_parent.is_trashed != node.is_trashed:
                raise ValueError("cannot move accross trash")
            if not new_parent.is_directory:
                raise ValueError("new_parent is not a directory")
            if await _contains(self, node, new_parent):
                raise ValueError("new_parent is a descendant of the source node")

        return await self._fs.move(
            node,
            new_parent=new_parent,
            new_name=new_name,
            trashed=trashed,
        )

    @override
    async def sync(self) -> AsyncIterator[ChangeAction]:
        if not await self.is_authorized():
            raise UnauthorizedError()

        async with self._sync_lock:
            initial_cursor = await self._fs.get_initial_cursor()

            cursor = await self._ss.get_current_cursor()
            if not cursor:
                cursor = initial_cursor

            # no data before, get the root node and cache it
            if cursor == initial_cursor:
                node = await self._fs.get_root()
                await self._ss.set_root(node)

            async for changes, next_ in self._fs.get_changes(cursor):
                await self._ss.apply_changes(changes, next_)

                for change in changes:
                    yield change

    @override
    async def get_hasher_factory(self) -> CreateHasher:
        return await self._fs.get_hasher_factory()

    @override
    async def is_authorized(self) -> bool:
        return await self._fs.is_authorized()

    @override
    async def get_oauth_url(self) -> str:
        return await self._fs.get_oauth_url()

    @override
    async def set_oauth_token(self, token: str):
        return await self._fs.set_oauth_token(token)


def _in_ancestor_set(
    table: dict[str, Node], node: Node, ancestor_set: set[str]
) -> bool:
    if node.parent_id is None:
        return False
    parent = table.get(node.parent_id, None)
    if not parent:
        return False
    if parent.id in ancestor_set:
        return True
    included = _in_ancestor_set(table, parent, ancestor_set)
    if included:
        ancestor_set.add(parent.id)
    return included


async def _contains(drive: Drive, ancestor: Node, node: Node) -> bool:
    visited: set[str] = set()
    while True:
        if ancestor.id == node.id:
            # meet the ancestor
            return True
        if not node.parent_id:
            # reached the root but never meet the ancestor
            return False

        visited.add(node.id)
        node = await drive.get_node_by_id(node.parent_id)
        if node.id in visited:
            raise RuntimeError("detected node cycle")

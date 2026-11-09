from asyncio import Lock, Queue, TaskGroup
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import tee
from pathlib import PurePath
from typing import override

from ._cache._service import SqliteCache, create_cache
from .exceptions import (
    AuthenticationError,
    InvalidServiceError,
    NodeExistsError,
    NodeNotFoundError,
)
from .lib import (
    else_none,
    is_valid_name,
    normalize_path,
)
from .types import (
    Backend,
    BackendFactory,
    ChangeAction,
    CreateHasher,
    CreateService,
    CreateServiceMiddleware,
    Drive,
    MediaInfo,
    Node,
    NodeQuery,
    NodeSummary,
    ReadableFile,
    Service,
    SourceConfig,
    WritableFile,
)


_API_VERSION = 7

_VIRTUAL_ROOT_ID = "__virtual_root__"


def compose_service[T: Service](
    base: CreateService[T],
    *middleware: CreateServiceMiddleware[T],
) -> CreateService[T]:
    @asynccontextmanager
    async def _factory() -> AsyncGenerator[T]:
        async with AsyncExitStack() as stack:
            service = await stack.enter_async_context(base())
            if service.api_version != _API_VERSION:
                raise InvalidServiceError(
                    f"invalid version: required {_API_VERSION}, got {service.api_version}"
                )
            for mw in middleware:
                service = await stack.enter_async_context(mw(service))
                if service.api_version != _API_VERSION:
                    raise InvalidServiceError(
                        f"invalid version: required {_API_VERSION}, got {service.api_version}"
                    )
            yield service

    return _factory


@asynccontextmanager
async def create_multi_drive(
    sources: Sequence[SourceConfig],
) -> AsyncGenerator[Drive]:
    """Create a configured Drive instance for cloud storage operations.

    Accepts a sequence of SourceConfig objects. With a single source,
    yields a single-mode Drive (paths unchanged). With multiple sources,
    yields a multi-mode Drive where each source's paths are prefixed with
    its name (e.g., /google/docs/file.txt).

    Args:
        sources: One or more SourceConfig objects describing storage backends.

    Yields:
        A fully configured Drive instance ready for operations.

    Raises:
        InvalidServiceError: If any service reports an incompatible API version.
        ValueError: If sources is empty or has duplicate names.
    """
    if not sources:
        raise ValueError("at least one source is required")
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate source names: {names}")

    if len(sources) == 1:
        source = sources[0]
        async with (
            create_cache(source.cache_path) as cache,
            _create_service(source.backend) as backend,
        ):
            yield _SingleDrive(backend=backend, cache=cache)
    else:
        async with AsyncExitStack() as stack:
            source_states: dict[str, _SourceState] = {}
            for source in sources:
                cache = await stack.enter_async_context(create_cache(source.cache_path))
                backend = await stack.enter_async_context(
                    _create_service(source.backend)
                )
                source_states[source.name] = _SourceState(backend=backend, cache=cache)
            yield _MultiDrive(source_states)


@asynccontextmanager
async def create_drive(
    *,
    backend: BackendFactory,
    cache_path: str,
) -> AsyncGenerator[Drive]:
    """Create a configured Drive instance for single-source usage.

    Args:
        backend: Factory function returning a Backend context manager.
                 Also accepts the output of compose_service().
        cache_path: Path to the SQLite file that caches node metadata.

    Yields:
        A fully configured Drive instance ready for operations.

    Raises:
        InvalidServiceError: If the service reports an incompatible API version.
    """
    async with create_multi_drive(
        sources=[SourceConfig(name="", backend=backend, cache_path=cache_path)]
    ) as drive:
        yield drive


class _SingleDrive(Drive):
    def __init__(
        self,
        *,
        backend: Backend,
        cache: SqliteCache,
    ) -> None:
        self._sync_lock = Lock()
        self._backend = backend
        self._cache = cache

    @override
    async def get_root(self) -> Node:
        return await self._cache.get_root()

    @override
    async def get_node_by_id(self, node_id: str) -> Node:
        return await self._cache.get_node_by_id(node_id)

    @override
    async def get_node_by_path(self, path: PurePath) -> Node:
        path = normalize_path(path)
        return await self._cache.get_node_by_path(path)

    @override
    async def resolve_path(self, node: Node) -> PurePath:
        return await self._cache.resolve_path_by_id(node.id)

    @override
    async def get_child_by_name(
        self,
        name: str,
        parent: Node,
    ) -> Node:
        return await self._cache.get_child_by_name(name, parent.id)

    @override
    async def get_children(self, parent: Node) -> list[Node]:
        return await self._cache.get_children_by_id(parent.id)

    @override
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        rv = await self._cache.get_trashed_nodes()
        if flatten:
            return rv

        ancestor_set = set(_.id for _ in rv if _.is_directory)
        if not ancestor_set:
            return rv

        table = {_.id: _ for _ in rv}
        return [_ for _ in rv if not _in_ancestor_set(table, _, ancestor_set)]

    @override
    async def query_nodes(self, query: NodeQuery) -> list[Node]:
        return await self._cache.query_nodes(query)

    @override
    async def summarize_nodes(self, query: NodeQuery) -> NodeSummary:
        if query.limit is not None:
            raise ValueError("summaries do not support limit")
        return await self._cache.summarize_nodes(query)

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
        if not await self.is_authenticated():
            raise AuthenticationError()

        if not exist_ok:
            node = await else_none(
                self.get_child_by_name(
                    name,
                    parent,
                )
            )
            if node:
                raise NodeExistsError(node)

        return await self._backend.create_directory(
            name,
            parent,
            exist_ok=exist_ok,
            private=None,
        )

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncGenerator[ReadableFile]:
        # sanity check
        if node.is_directory:
            raise ValueError("node should be a file")
        if not await self.is_authenticated():
            raise AuthenticationError()

        async with self._backend.download_file(node) as file:
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
    ) -> AsyncGenerator[WritableFile]:
        # sanity check
        if not parent.is_directory:
            raise ValueError("parent is not a directory")
        if not name:
            raise ValueError("directory name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in directory name")
        if not await self.is_authenticated():
            raise AuthenticationError()

        node = await else_none(self.get_child_by_name(name, parent))
        if node:
            raise NodeExistsError(node)

        async with self._backend.upload_file(
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
        if not await self.is_authenticated():
            raise AuthenticationError()

        await self._backend.purge_trash()

    @override
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        if not await self.is_authenticated():
            raise AuthenticationError()

        await self._backend.delete(node, permanent=permanent)

    @override
    async def restore(self, node: Node) -> Node:
        if not await self.is_authenticated():
            raise AuthenticationError()

        return await self._backend.restore(node)

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
    ) -> Node:
        # sanity check
        if not await self.is_authenticated():
            raise AuthenticationError()

        if not new_parent and not new_name:
            raise ValueError("nothing to move")

        if new_name and not is_valid_name(new_name):
            raise ValueError("no `/` or `\\` allowed in file name")

        root_node = await self.get_root()
        if node.id == root_node.id:
            raise ValueError("root node is immutable")

        if new_parent:
            if not new_parent.is_directory:
                raise ValueError("new_parent is not a directory")
            if await _contains(self, node, new_parent):
                raise ValueError("new_parent is a descendant of the source node")

        return await self._backend.move(
            node,
            new_parent=new_parent,
            new_name=new_name,
        )

    @override
    async def sync(self) -> AsyncIterator[ChangeAction]:
        if not await self.is_authenticated():
            raise AuthenticationError()

        async with self._sync_lock:
            initial_cursor = await self._backend.get_initial_cursor()

            cursor = await self._cache.get_current_cursor()
            if not cursor:
                cursor = initial_cursor

            # no data before, get the root node and cache it
            if cursor == initial_cursor:
                node = await self._backend.get_root()
                await self._cache.set_root(node)

            async for changes, next_ in self._backend.get_changes(cursor):
                await self._cache.apply_changes(changes, next_)

                for change in changes:
                    yield change

    @override
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        return await self._backend.get_hasher_factory(node)

    @override
    async def is_authenticated(self) -> bool:
        return await self._backend.is_authenticated()

    @override
    async def authenticate(self) -> None:
        return await self._backend.authenticate()


@dataclass
class _SourceState:
    backend: Backend
    cache: SqliteCache
    lock: Lock = field(default_factory=Lock)


class _MultiDrive(Drive):
    def __init__(self, sources: dict[str, _SourceState]) -> None:
        self._sources = sources

    @override
    async def get_root(self) -> Node:
        return _make_virtual_root()

    @override
    async def get_node_by_id(self, node_id: str) -> Node:
        if node_id == _VIRTUAL_ROOT_ID:
            return _make_virtual_root()
        source_name, original_id = _parse_id(node_id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node_id)
        state = self._sources[source_name]
        node = await state.cache.get_node_by_id(original_id)
        return _scope_node(source_name, node)

    @override
    async def get_node_by_path(self, path: PurePath) -> Node:
        path = normalize_path(path)
        parts = path.parts  # ('/', ...) or ('/',)

        if len(parts) == 1:
            return _make_virtual_root()

        source_name = parts[1]
        if source_name not in self._sources:
            raise NodeNotFoundError(str(path))
        state = self._sources[source_name]

        if len(parts) == 2:
            root = await state.cache.get_root()
            return replace(_scope_node(source_name, root), name=source_name)

        remaining = PurePath("/", *parts[2:])
        node = await state.cache.get_node_by_path(remaining)
        return _scope_node(source_name, node)

    @override
    async def resolve_path(self, node: Node) -> PurePath:
        if node.id == _VIRTUAL_ROOT_ID:
            return PurePath("/")
        source_name, original_id = _parse_id(node.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        if node.parent_id == _VIRTUAL_ROOT_ID:
            return PurePath("/", source_name)
        state = self._sources[source_name]
        path = await state.cache.resolve_path_by_id(original_id)
        return PurePath("/", source_name, *path.parts[1:])

    @override
    async def get_child_by_name(self, name: str, parent: Node) -> Node:
        if parent.id == _VIRTUAL_ROOT_ID:
            if name not in self._sources:
                raise NodeNotFoundError(name)
            state = self._sources[name]
            root = await state.cache.get_root()
            return replace(_scope_node(name, root), name=name)
        source_name, original_id = _parse_id(parent.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(parent.id)
        state = self._sources[source_name]
        child = await state.cache.get_child_by_name(name, original_id)
        return _scope_node(source_name, child)

    @override
    async def get_children(self, parent: Node) -> list[Node]:
        if parent.id == _VIRTUAL_ROOT_ID:
            roots: list[Node] = []
            for source_name, state in self._sources.items():
                root = await state.cache.get_root()
                roots.append(replace(_scope_node(source_name, root), name=source_name))
            return roots
        source_name, original_id = _parse_id(parent.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(parent.id)
        state = self._sources[source_name]
        children = await state.cache.get_children_by_id(original_id)
        return [_scope_node(source_name, child) for child in children]

    @override
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        all_nodes: list[Node] = []
        for source_name, state in self._sources.items():
            trashed = await state.cache.get_trashed_nodes()
            scoped = [_scope_node(source_name, n) for n in trashed]
            if flatten:
                all_nodes.extend(scoped)
            else:
                ancestor_set = set(n.id for n in scoped if n.is_directory)
                if not ancestor_set:
                    all_nodes.extend(scoped)
                else:
                    table = {n.id: n for n in scoped}
                    all_nodes.extend(
                        n
                        for n in scoped
                        if not _in_ancestor_set(table, n, ancestor_set)
                    )
        return all_nodes

    @override
    async def query_nodes(self, query: NodeQuery) -> list[Node]:
        targets = self._route_query(query)

        results: list[Node] = []
        for source_name, source_query in targets:
            state = self._sources[source_name]
            nodes = await state.cache.query_nodes(source_query)
            results.extend(_scope_node(source_name, n) for n in nodes)
            if query.limit is not None and len(results) >= query.limit:
                break
        return results[: query.limit]

    def _route_query(self, query: NodeQuery) -> list[tuple[str, NodeQuery]]:
        if query.ancestor_id is None or query.ancestor_id == _VIRTUAL_ROOT_ID:
            # every source is under the virtual root
            unscoped = replace(query, ancestor_id=None)
            return [(name, unscoped) for name in self._sources]
        else:
            source_name, original_id = _parse_id(query.ancestor_id)
            if source_name not in self._sources:
                raise NodeNotFoundError(query.ancestor_id)
            return [(source_name, replace(query, ancestor_id=original_id))]

    @override
    async def summarize_nodes(self, query: NodeQuery) -> NodeSummary:
        if query.limit is not None:
            raise ValueError("summaries do not support limit")
        count = total_size = 0
        for source_name, source_query in self._route_query(query):
            summary = await self._sources[source_name].cache.summarize_nodes(
                source_query
            )
            count += summary.count
            total_size += summary.total_size
        return NodeSummary(count=count, total_size=total_size)

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
        if not parent.is_directory:
            raise ValueError("parent is not a directory")
        if not name:
            raise ValueError("directory name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in directory name")
        if not await self.is_authenticated():
            raise AuthenticationError()

        if parent.id == _VIRTUAL_ROOT_ID:
            raise ValueError("cannot create directory in virtual root")

        source_name, _ = _parse_id(parent.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(parent.id)
        state = self._sources[source_name]
        unscoped_parent = _unscope_node(source_name, parent)

        if not exist_ok:
            node = await else_none(self.get_child_by_name(name, parent))
            if node:
                raise NodeExistsError(node)

        result = await state.backend.create_directory(
            name, unscoped_parent, exist_ok=exist_ok, private=None
        )
        return _scope_node(source_name, result)

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncGenerator[ReadableFile]:
        if node.is_directory:
            raise ValueError("node should be a file")
        if not await self.is_authenticated():
            raise AuthenticationError()

        source_name, _ = _parse_id(node.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        state = self._sources[source_name]
        unscoped_node = _unscope_node(source_name, node)

        async with state.backend.download_file(unscoped_node) as file:
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
    ) -> AsyncGenerator[WritableFile]:
        if not parent.is_directory:
            raise ValueError("parent is not a directory")
        if not name:
            raise ValueError("directory name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in directory name")
        if not await self.is_authenticated():
            raise AuthenticationError()

        if parent.id == _VIRTUAL_ROOT_ID:
            raise ValueError("cannot upload to virtual root")

        source_name, _ = _parse_id(parent.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(parent.id)
        state = self._sources[source_name]
        unscoped_parent = _unscope_node(source_name, parent)

        node = await else_none(self.get_child_by_name(name, parent))
        if node:
            raise NodeExistsError(node)

        async with state.backend.upload_file(
            name,
            unscoped_parent,
            size=size,
            mime_type=mime_type,
            media_info=media_info,
            private=None,
        ) as file:
            yield _ScopedWritableFile(source_name, file)

    @override
    async def purge_trash(self) -> None:
        if not await self.is_authenticated():
            raise AuthenticationError()
        for state in self._sources.values():
            await state.backend.purge_trash()

    @override
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        if not await self.is_authenticated():
            raise AuthenticationError()

        source_name, _ = _parse_id(node.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        state = self._sources[source_name]
        unscoped_node = _unscope_node(source_name, node)
        await state.backend.delete(unscoped_node, permanent=permanent)

    @override
    async def restore(self, node: Node) -> Node:
        if not await self.is_authenticated():
            raise AuthenticationError()

        source_name, _ = _parse_id(node.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        state = self._sources[source_name]
        unscoped_node = _unscope_node(source_name, node)
        result = await state.backend.restore(unscoped_node)
        return _scope_node(source_name, result)

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
    ) -> Node:
        if not await self.is_authenticated():
            raise AuthenticationError()

        if not new_parent and not new_name:
            raise ValueError("nothing to move")

        if new_name and not is_valid_name(new_name):
            raise ValueError("no `/` or `\\` allowed in file name")

        if node.id == _VIRTUAL_ROOT_ID:
            raise ValueError("root node is immutable")

        if node.parent_id == _VIRTUAL_ROOT_ID:
            raise ValueError("source-root node is immutable")

        source_name, _ = _parse_id(node.id)

        if new_parent:
            if new_parent.id == _VIRTUAL_ROOT_ID:
                raise ValueError("cannot move to virtual root")
            new_parent_source, _ = _parse_id(new_parent.id)
            if new_parent_source != source_name:
                raise ValueError("cannot move across sources")
            if not new_parent.is_directory:
                raise ValueError("new_parent is not a directory")
            if await _contains(self, node, new_parent):
                raise ValueError("new_parent is a descendant of the source node")

        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        state = self._sources[source_name]
        unscoped_node = _unscope_node(source_name, node)
        unscoped_new_parent = (
            _unscope_node(source_name, new_parent) if new_parent else None
        )

        result = await state.backend.move(
            unscoped_node,
            new_parent=unscoped_new_parent,
            new_name=new_name,
        )
        return _scope_node(source_name, result)

    @override
    async def sync(self) -> AsyncIterator[ChangeAction]:
        if not await self.is_authenticated():
            raise AuthenticationError()

        queue: Queue[ChangeAction | None] = Queue()
        n_sources = len(self._sources)

        async def sync_source(source_name: str, state: _SourceState) -> None:
            async with state.lock:
                backend = state.backend
                cache = state.cache

                initial_cursor = await backend.get_initial_cursor()

                cursor = await cache.get_current_cursor()
                if not cursor:
                    cursor = initial_cursor

                if cursor == initial_cursor:
                    node = await backend.get_root()
                    await cache.set_root(node)

                async for changes, next_ in backend.get_changes(cursor):
                    await cache.apply_changes(changes, next_)

                    for change in changes:
                        await queue.put(_scope_change(source_name, change))
            await queue.put(None)

        async with TaskGroup() as tg:
            for source_name, state in self._sources.items():
                tg.create_task(sync_source(source_name, state))

            done = 0
            while done < n_sources:
                item = await queue.get()
                if item is None:
                    done += 1
                else:
                    yield item

    @override
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        source_name, _ = _parse_id(node.id)
        if source_name not in self._sources:
            raise NodeNotFoundError(node.id)
        state = self._sources[source_name]
        unscoped_node = _unscope_node(source_name, node)
        return await state.backend.get_hasher_factory(unscoped_node)

    @override
    async def is_authenticated(self) -> bool:
        for state in self._sources.values():
            if not await state.backend.is_authenticated():
                return False
        return True

    @override
    async def authenticate(self) -> None:
        for state in self._sources.values():
            await state.backend.authenticate()


class _ScopedWritableFile(WritableFile):
    def __init__(self, source: str, file: WritableFile) -> None:
        self._source = source
        self._file = file

    @override
    async def tell(self) -> int:
        return await self._file.tell()

    @override
    async def seek(self, offset: int) -> int:
        return await self._file.seek(offset)

    @override
    async def write(self, chunk: bytes) -> int:
        return await self._file.write(chunk)

    @override
    async def flush(self) -> None:
        await self._file.flush()

    @override
    async def node(self) -> Node:
        node = await self._file.node()
        return _scope_node(self._source, node)


@asynccontextmanager
async def _create_service[T: Service](create_service: CreateService[T]):
    async with create_service() as service:
        if service.api_version != _API_VERSION:
            raise InvalidServiceError(
                f"invalid version: required {_API_VERSION}, got {service.api_version}"
            )
        yield service


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


def _scope_id(source: str, original_id: str) -> str:
    return f"{source}:{original_id}"


def _parse_id(scoped_id: str) -> tuple[str, str]:
    source, _, original_id = scoped_id.partition(":")
    return source, original_id


def _scope_node(source: str, node: Node) -> Node:
    new_parent_id = (
        _VIRTUAL_ROOT_ID
        if node.parent_id is None
        else _scope_id(source, node.parent_id)
    )
    return replace(node, id=_scope_id(source, node.id), parent_id=new_parent_id)


def _unscope_node(source: str, node: Node) -> Node:
    _, original_id = _parse_id(node.id)
    if node.parent_id == _VIRTUAL_ROOT_ID:
        original_parent_id = None
    elif node.parent_id is None:
        original_parent_id = None
    else:
        _, original_parent_id = _parse_id(node.parent_id)
    return replace(node, id=original_id, parent_id=original_parent_id)


def _scope_change(source: str, change: ChangeAction) -> ChangeAction:
    match change:
        case (True, id_):
            return (True, _scope_id(source, id_))
        case (False, node):
            return (False, _scope_node(source, node))


def _make_virtual_root() -> Node:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return Node(
        id=_VIRTUAL_ROOT_ID,
        parent_id=None,
        name="",
        is_directory=True,
        is_trashed=False,
        created_time=epoch,
        modified_time=epoch,
        changed_time=epoch,
        mime_type="",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )

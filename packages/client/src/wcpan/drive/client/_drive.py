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
    StaleCursorError,
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


_API_VERSION = 6

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
        ValueError: If sources is empty, has duplicate names, or a name
            contains ``:``.
    """
    if not sources:
        raise ValueError("at least one source is required")
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate source names: {names}")
    if any(":" in _ for _ in names):
        # scoped node IDs are "<source>:<id>", split at the first colon
        raise ValueError(f"source names must not contain ':': {names}")

    scoped = len(sources) > 1
    async with AsyncExitStack() as stack:
        states: dict[str, _Source] = {}
        for source in sources:
            cache = await stack.enter_async_context(create_cache(source.cache_path))
            backend = await stack.enter_async_context(_create_service(source.backend))
            states[source.name] = _Source(
                name=source.name, backend=backend, cache=cache, scoped=scoped
            )
        yield _Drive(states, scoped=scoped)


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


@dataclass
class _Source:
    """One backend and its cache, with the ID scheme the drive shows for it.

    A scoped source prefixes node IDs with its name and hangs its root under
    the virtual root; an unscoped one (the only source of a drive) shows the
    backend's nodes unchanged.
    """

    name: str
    backend: Backend
    cache: SqliteCache
    scoped: bool
    lock: Lock = field(default_factory=Lock)

    def scope(self, node: Node) -> Node:
        return _scope_node(self.name, node) if self.scoped else node

    def unscope(self, node: Node) -> Node:
        return _unscope_node(self.name, node) if self.scoped else node

    def scope_change(self, change: ChangeAction) -> ChangeAction:
        return _scope_change(self.name, change) if self.scoped else change

    def scope_path(self, path: PurePath) -> PurePath:
        if not self.scoped:
            return path
        return PurePath("/", self.name, *path.parts[1:])

    async def get_root(self) -> Node:
        """The source root as the drive shows it, named after a scoped source."""
        root = self.scope(await self.cache.get_root())
        return replace(root, name=self.name) if self.scoped else root

    async def sync(self) -> AsyncIterator[ChangeAction]:
        async with self.lock:
            try:
                async for change in self._sync_once():
                    yield change
            except StaleCursorError:
                # Retry once: a fresh replay starts from the initial cursor,
                # which a backend never reports as stale.
                async for change in self._reset():
                    yield change
                async for change in self._sync_once():
                    yield change

    async def _sync_once(self) -> AsyncIterator[ChangeAction]:
        initial_cursor = await self.backend.get_initial_cursor()

        cursor = await self.cache.get_current_cursor()
        if not cursor:
            cursor = initial_cursor

        # no data before, get the root node and cache it
        if cursor == initial_cursor:
            node = await self.backend.get_root()
            await self.cache.set_root(node)

        async for changes, next_ in self.backend.get_changes(cursor):
            await self.cache.apply_changes(changes, next_)

            for change in changes:
                yield self.scope_change(change)

    async def _reset(self) -> AsyncIterator[ChangeAction]:
        """Announce every cached node as removed, then empty the cache.

        Change actions have no reset event, so consumers learn of the reset as
        removals followed by the full replay. The cache is emptied only after
        the removals are delivered, so an interrupted reset is redone by the
        next sync. The root is never announced by sync, so it is not removed.
        """
        root = await else_none(self.cache.get_root())
        for node_id in await self.cache.get_node_ids():
            if root is None or node_id != root.id:
                yield self.scope_change((True, node_id))
        await self.cache.reset()


class _Drive(Drive):
    """A Drive over one unscoped source, or several under a virtual root."""

    def __init__(self, sources: dict[str, _Source], *, scoped: bool) -> None:
        self._sources = sources
        self._scoped = scoped

    def _is_virtual_root(self, node_id: str | None) -> bool:
        return self._scoped and node_id == _VIRTUAL_ROOT_ID

    def _locate(self, node_id: str) -> tuple[_Source, str]:
        """The source holding *node_id*, and the ID its backend uses."""
        if not self._scoped:
            (source,) = self._sources.values()
            return source, node_id
        source_name, original_id = _parse_id(node_id)
        source = self._sources.get(source_name)
        if source is None:
            raise NodeNotFoundError(node_id)
        return source, original_id

    async def _require_authenticated(self) -> None:
        if not await self.is_authenticated():
            raise AuthenticationError()

    @override
    async def get_root(self) -> Node:
        if self._scoped:
            return _make_virtual_root()
        (source,) = self._sources.values()
        return await source.get_root()

    @override
    async def get_node_by_id(self, node_id: str) -> Node:
        if self._is_virtual_root(node_id):
            return _make_virtual_root()
        source, original_id = self._locate(node_id)
        return source.scope(await source.cache.get_node_by_id(original_id))

    @override
    async def get_node_by_path(self, path: PurePath) -> Node:
        path = normalize_path(path)
        if not self._scoped:
            (source,) = self._sources.values()
            return await source.cache.get_node_by_path(path)

        parts = path.parts  # ('/', ...) or ('/',)
        if len(parts) == 1:
            return _make_virtual_root()
        source = self._sources.get(parts[1])
        if source is None:
            raise NodeNotFoundError(str(path))
        if len(parts) == 2:
            return await source.get_root()
        remaining = PurePath("/", *parts[2:])
        return source.scope(await source.cache.get_node_by_path(remaining))

    @override
    async def resolve_path(self, node: Node) -> PurePath:
        if self._is_virtual_root(node.id):
            return PurePath("/")
        source, original_id = self._locate(node.id)
        if self._is_virtual_root(node.parent_id):
            return PurePath("/", source.name)
        path = await source.cache.resolve_path_by_id(original_id)
        return source.scope_path(path)

    @override
    async def get_child_by_name(self, name: str, parent: Node) -> Node:
        if self._is_virtual_root(parent.id):
            source = self._sources.get(name)
            if source is None:
                raise NodeNotFoundError(name)
            return await source.get_root()
        source, original_id = self._locate(parent.id)
        return source.scope(await source.cache.get_child_by_name(name, original_id))

    @override
    async def get_children(self, parent: Node) -> list[Node]:
        if self._is_virtual_root(parent.id):
            return [await _.get_root() for _ in self._sources.values()]
        source, original_id = self._locate(parent.id)
        children = await source.cache.get_children_by_id(original_id)
        return [source.scope(_) for _ in children]

    @override
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        rv: list[Node] = []
        for source in self._sources.values():
            trashed = [source.scope(_) for _ in await source.cache.get_trashed_nodes()]
            rv.extend(trashed if flatten else _top_trashed(trashed))
        return rv

    @override
    async def query_nodes(self, query: NodeQuery) -> list[Node]:
        results: list[Node] = []
        for source, source_query in self._route_query(query):
            nodes = await source.cache.query_nodes(source_query)
            results.extend(source.scope(_) for _ in nodes)
            if query.limit is not None and len(results) >= query.limit:
                break
        return results[: query.limit]

    @override
    async def summarize_nodes(self, query: NodeQuery) -> NodeSummary:
        if query.limit is not None:
            raise ValueError("summaries do not support limit")
        count = total_size = 0
        for source, source_query in self._route_query(query):
            summary = await source.cache.summarize_nodes(source_query)
            count += summary.count
            total_size += summary.total_size
        return NodeSummary(count=count, total_size=total_size)

    def _route_query(self, query: NodeQuery) -> list[tuple[_Source, NodeQuery]]:
        if query.ancestor_id is None or self._is_virtual_root(query.ancestor_id):
            # every source is under the root
            unscoped = replace(query, ancestor_id=None)
            return [(_, unscoped) for _ in self._sources.values()]
        source, original_id = self._locate(query.ancestor_id)
        return [(source, replace(query, ancestor_id=original_id))]

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
        await self._require_authenticated()

        if self._is_virtual_root(parent.id):
            raise ValueError("cannot create directory in virtual root")
        source, _ = self._locate(parent.id)

        if not exist_ok:
            node = await else_none(self.get_child_by_name(name, parent))
            if node:
                raise NodeExistsError(node)

        rv = await source.backend.create_directory(
            name,
            source.unscope(parent),
            exist_ok=exist_ok,
            private=None,
        )
        return source.scope(rv)

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncGenerator[ReadableFile]:
        # sanity check
        if node.is_directory:
            raise ValueError("node should be a file")
        await self._require_authenticated()

        source, _ = self._locate(node.id)
        async with source.backend.download_file(source.unscope(node)) as file:
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
            raise ValueError("file name is empty")
        if not is_valid_name(name):
            raise ValueError("no `/` or `\\` allowed in file name")
        await self._require_authenticated()

        if self._is_virtual_root(parent.id):
            raise ValueError("cannot upload to virtual root")
        source, _ = self._locate(parent.id)

        node = await else_none(self.get_child_by_name(name, parent))
        if node:
            raise NodeExistsError(node)

        async with source.backend.upload_file(
            name,
            source.unscope(parent),
            size=size,
            mime_type=mime_type,
            media_info=media_info,
            private=None,
        ) as file:
            yield _ScopedWritableFile(source, file) if source.scoped else file

    @override
    async def purge_trash(self) -> None:
        await self._require_authenticated()
        for source in self._sources.values():
            await source.backend.purge_trash()

    @override
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        await self._require_authenticated()
        source, _ = self._locate(node.id)
        await source.backend.delete(source.unscope(node), permanent=permanent)

    @override
    async def restore(self, node: Node) -> Node:
        await self._require_authenticated()
        source, _ = self._locate(node.id)
        return source.scope(await source.backend.restore(source.unscope(node)))

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
    ) -> Node:
        # sanity check
        await self._require_authenticated()

        if not new_parent and not new_name:
            raise ValueError("nothing to move")

        if new_name and not is_valid_name(new_name):
            raise ValueError("no `/` or `\\` allowed in file name")

        root = await self.get_root()
        if node.id == root.id:
            raise ValueError("root node is immutable")
        if self._is_virtual_root(node.parent_id):
            raise ValueError("source-root node is immutable")

        source, _ = self._locate(node.id)

        if new_parent:
            if self._is_virtual_root(new_parent.id):
                raise ValueError("cannot move to virtual root")
            new_parent_source, _ = self._locate(new_parent.id)
            if new_parent_source is not source:
                raise ValueError("cannot move across sources")
            if not new_parent.is_directory:
                raise ValueError("new_parent is not a directory")
            if await _contains(self, node, new_parent):
                raise ValueError("new_parent is a descendant of the source node")

        rv = await source.backend.move(
            source.unscope(node),
            new_parent=source.unscope(new_parent) if new_parent else None,
            new_name=new_name,
        )
        return source.scope(rv)

    @override
    async def sync(self) -> AsyncIterator[ChangeAction]:
        await self._require_authenticated()

        if len(self._sources) == 1:
            # pull pages only as fast as the caller consumes them
            (source,) = self._sources.values()
            async for change in source.sync():
                yield change
            return

        queue: Queue[ChangeAction | None] = Queue()

        async def pump(source: _Source) -> None:
            async for change in source.sync():
                await queue.put(change)
            await queue.put(None)

        async with TaskGroup() as tg:
            for source in self._sources.values():
                tg.create_task(pump(source))

            done = 0
            while done < len(self._sources):
                item = await queue.get()
                if item is None:
                    done += 1
                else:
                    yield item

    @override
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        source, _ = self._locate(node.id)
        return await source.backend.get_hasher_factory(source.unscope(node))

    @override
    async def is_authenticated(self) -> bool:
        for source in self._sources.values():
            if not await source.backend.is_authenticated():
                return False
        return True

    @override
    async def authenticate(self) -> None:
        for source in self._sources.values():
            await source.backend.authenticate()


class _ScopedWritableFile(WritableFile):
    def __init__(self, source: _Source, file: WritableFile) -> None:
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
        return self._source.scope(await self._file.node())


@asynccontextmanager
async def _create_service[T: Service](create_service: CreateService[T]):
    async with create_service() as service:
        if service.api_version != _API_VERSION:
            raise InvalidServiceError(
                f"invalid version: required {_API_VERSION}, got {service.api_version}"
            )
        yield service


def _top_trashed(nodes: list[Node]) -> list[Node]:
    """Drop trashed nodes that sit inside a trashed directory of *nodes*."""
    ancestor_set = set(_.id for _ in nodes if _.is_directory)
    if not ancestor_set:
        return nodes
    table = {_.id: _ for _ in nodes}
    return [_ for _ in nodes if not _in_ancestor_set(table, _, ancestor_set)]


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

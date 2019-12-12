from typing import List, AsyncGenerator, Tuple, Dict, Optional
import asyncio
import concurrent.futures
import contextlib
import functools
import importlib
import pathlib

import yaml

from .cache import Cache
from .exceptions import (
    DownloadError,
    InvalidMiddlewareError,
    InvalidRemoteDriverError,
    LineageError,
    NodeConflictedError,
    NodeNotFoundError,
    ParentIsNotFolderError,
    RootNodeError,
    TrashedNodeError,
    UploadError,
)
from .types import (
    ChangeDict,
    CreateFolderFunction,
    DownloadFunction,
    GetHasherFunction,
    Node,
    NodeDict,
    RenameNodeFunction,
    UploadFunction,
)
from .abc import ReadableFile, WritableFile, Hasher, RemoteDriver, Middleware
from .util import (
    create_executor,
    get_default_config_path,
    get_default_data_path,
    resolve_path,
)


DRIVER_VERSION = 1


class Context(object):

    def __init__(self,
        config_path: str,
        data_path: str,
        database_dsn: str,
        driver_class: RemoteDriver,
        middleware_list: List[Middleware],
        pool: Optional[concurrent.futures.Executor],
    ) -> None:
        self._config_path = config_path
        self._data_path = data_path
        self._database_dsn = database_dsn
        self._driver_class = driver_class
        self._middleware_list = middleware_list
        self._pool = pool

    @property
    def database_dsn(self):
        return self._database_dsn

    @property
    def pool(self) -> Optional[concurrent.futures.Executor]:
        return self._pool

    def create_remote_driver(self) -> RemoteDriver:
        return self._driver_class(
            self._config_path,
            self._data_path,
        )

    # pop order
    async def decode_dict(self, dict_: NodeDict) -> NodeDict:
        for middleware in reversed(self._middleware_list):
            dict_ = await middleware.decode_dict(dict_)
        return dict_

    # push order
    def rename_node(self, fn: RenameNodeFunction) -> RenameNodeFunction:
        fn = functools.reduce(
            lambda rn, middleware: functools.partial(middleware.rename_node, rn),
            self._middleware_list,
            fn,
        )
        return fn

    # pop order
    def download(self, fn: DownloadFunction) -> DownloadFunction:
        fn = functools.reduce(
            lambda d, middleware: functools.partial(middleware.download, d),
            reversed(self._middleware_list),
            fn,
        )
        return fn

    # push order
    def upload(self, fn: UploadFunction) -> UploadFunction:
        fn = functools.reduce(
            lambda u, middleware: functools.partial(middleware.upload, u),
            self._middleware_list,
            fn,
        )
        return fn

    # push order
    def create_folder(self, fn: CreateFolderFunction) -> CreateFolderFunction:
        fn = functools.reduce(
            lambda cf, middleware: functools.partial(middleware.create_folder, cf),
            self._middleware_list,
            fn,
        )
        return fn

    # push order
    def get_hasher(self, fn: GetHasherFunction) -> GetHasherFunction:
        fn = functools.reduce(
            lambda gh, middleware: functools.partial(middleware.get_hasher, gh),
            self._middleware_list,
            fn,
        )
        return fn


class Drive(object):

    def __init__(self, context: Context) -> None:
        self._context = context
        self._sync_lock = asyncio.Lock()

        self._remote = None

        self._pool = None
        self._db = None

        self._raii = None

    async def __aenter__(self) -> 'Drive':
        async with contextlib.AsyncExitStack() as stack:
            if not self._context.pool:
                self._pool = stack.enter_context(create_executor())
            else:
                self._pool = self._context.pool

            self._remote = await stack.enter_async_context(
                self._context.create_remote_driver()
            )

            dsn = self._context.database_dsn
            self._db = await stack.enter_async_context(Cache(dsn, self._pool))

            self._raii = stack.pop_all()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._raii.aclose()
        self._remote = None
        self._pool = None
        self._db = None
        self._raii = None

    async def get_root_node(self) -> Node:
        return await self._db.get_root_node()

    async def get_node_by_id(self, node_id: str) -> Node:
        return await self._db.get_node_by_id(node_id)

    async def get_node_by_path(self, path: str) -> Node:
        return await self._db.get_node_by_path(path)

    async def get_path(self, node: Node) -> str:
        return await self._db.get_path_by_id(node.id_)

    async def get_path_by_id(self, node_id: str) -> str:
        return await self._db.get_path_by_id(node_id)

    async def get_node_by_name_from_parent_id(self,
        name: str,
        parent_id: str,
    ) -> Node:
        return await self._db.get_node_by_name_from_parent_id(name, parent_id)

    async def get_node_by_name_from_parent(self,
        name: str,
        parent: Node,
    ) -> Node:
        return await self._db.get_node_by_name_from_parent_id(name, parent.id_)

    async def get_children(self, node: Node) -> List[Node]:
        return await self._db.get_children_by_id(node.id_)

    async def get_children_by_id(self, node_id: str) -> List[Node]:
        return await self._db.get_children_by_id(node_id)

    async def find_nodes_by_regex(self, pattern: str) -> List[Node]:
        return await self._db.find_nodes_by_regex(pattern)

    async def find_duplicate_nodes(self) -> List[Node]:
        return await self._db.find_duplicate_nodes()

    async def find_orphan_nodes(self) -> List[Node]:
        return await self._db.find_orphan_nodes()

    async def find_multiple_parents_nodes(self) -> List[Node]:
        return await self._db.find_multiple_parents_nodes()

    async def walk(self,
        node: Node,
    ) -> AsyncGenerator[Tuple[Node, List[Node], List[Node]], None]:
        if not node.is_folder:
            return
        q = [node]
        while q:
            node = q[0]
            del q[0]
            children = await self.get_children(node)
            folders = list(filter(lambda _: _.is_folder, children))
            files = list(filter(lambda _: _.is_file, children))
            yield node, folders, files
            q.extend(folders)

    async def create_folder(self,
        parent_node: Node,
        folder_name: str,
        exist_ok: bool = False,
    ) -> Node:
        # sanity check
        if not parent_node:
            raise TypeError('invalid parent node')
        if not parent_node.is_folder:
            raise ParentIsNotFolderError('invalid parent node')
        if not folder_name:
            raise TypeError('invalid folder name')

        node = await self.get_node_by_name_from_parent(folder_name, parent_node)
        if node:
            raise NodeConflictedError(node)

        fn = self._context.create_folder(self._remote.create_folder)
        return await fn(parent_node, folder_name, None, exist_ok)

    async def download_by_id(self, node_id: str) -> ReadableFile:
        node = await self.get_node_by_id(node_id)
        return await self.download(node)

    async def download(self, node: Node) -> ReadableFile:
        # sanity check
        if not node:
            raise TypeError('node is none')
        if node.is_folder:
            raise DownloadError('node should be a file')

        fn = self._context.download(self._remote.download)
        return await fn(node)

    async def upload_by_id(self,
        parent_id: str,
        file_name: str,
        file_size: int = None,
        mime_type: str = None,
    ) -> WritableFile:
        node = await self.get_node_by_id(parent_id)
        return await self.upload(node, file_name, file_size, mime_type)

    async def upload(self,
        parent_node: Node,
        file_name: str,
        file_size: int = None,
        mime_type: str = None,
    ) -> WritableFile:
        # sanity check
        if not parent_node:
            raise TypeError('invalid parent node')
        if not parent_node.is_folder:
            raise ParentIsNotFolderError('invalid parent node')
        if not file_name:
            raise TypeError('invalid file name')

        node = await self.get_node_by_name_from_parent(file_name, parent_node)
        if node:
            raise NodeConflictedError(node)

        fn = self._context.upload(self._remote.upload)
        return await fn(parent_node, file_name, file_size, mime_type, None)

    async def trash_node_by_id(self, node_id: str) -> None:
        node = await self.get_node_by_id(node_id)
        await self.trash_node(node)

    async def trash_node(self, node: Node) -> None:
        # sanity check
        if not node:
            raise TypeError('source node is none')
        root_node = await self.get_root_node()
        if root_node.id_ == node.id_:
            raise RootNodeError('cannot trash root node')
        await self._remote.trash_node(node)

    async def rename_node(self,
        node: Node,
        new_parent: Node = None,
        new_name: str = None,
    ) -> Node:
        # sanity check
        if not node:
            raise TypeError('source node is none')
        if node.trashed:
            raise TrashedNodeError('source node is in trash')
        root_node = await self.get_root_node()
        if node.id_ == root_node.id_:
            raise RootNodeError('source node is the root node')

        if not new_parent and not new_name:
            raise TypeError('need new_parent or new_name')

        if new_parent:
            if new_parent.trashed:
                raise TrashedNodeError('new_parent is in trash')
            if new_parent.is_file:
                raise ParentIsNotFolderError('new_parent is not a folder')
            ancestor = new_parent
            while True:
                if ancestor.id_ == node.id_:
                    raise LineageError('new_parent is a descendant of node')
                if not ancestor.parent_id:
                    break
                ancestor = await self.get_node_by_id(ancestor.parent_id)

        fn = self._context.rename_node(self._remote.rename_node)
        return await fn(node, new_parent, new_name)

    async def rename_node_by_path(self, src_path: str, dst_path: str) -> Node:
        '''
        Rename or move `src_path` to `dst_path`. `dst_path` can be a file name
        or an absolute path.

        If `dst_path` is a file and already exists, `NodeConflictedError` will
        be raised.

        If `dst_path` is a folder, `src_path` will be moved to there without
        renaming.

        If `dst_path` does not exist yet, `src_path` will be moved and rename to
        `dst_path`.
        '''
        node = await self.get_node_by_path(src_path)
        if not node:
            raise NodeNotFoundError(src_path)

        src = pathlib.PurePath(src_path)
        dst = pathlib.PurePath(dst_path)

        # case 1 - move to a relative path
        if not dst.is_absolute():
            # case 1.1 - a name, not path
            if dst.name == dst_path:
                # case 1.1.1 - move to the same folder, do nothing
                if dst.name == '.':
                    return node
                # case 1.1.2 - rename only
                if dst.name != '..':
                    return await self.rename_node(node, None, dst.name)
                # case 1.1.3 - move to parent folder, the same as case 1.2

            # case 1.2 - a relative path, resolve to absolute path
            # NOTE pathlib.PurePath does not implement normalizing algorithm
            dst = resolve_path(src.parent, dst)

        # case 2 - move to an absolute path
        dst_node = await self.get_node_by_path(str(dst))
        # case 2.1 - the destination is empty
        if not dst_node:
            # move to the parent folder of the destination
            new_parent = await self.get_node_by_path(str(dst.parent))
            if not new_parent:
                raise LineageError(f'no direct path to {dst_path}')
            return await self.rename_node(node, new_parent, dst.name)
        # case 2.2 - the destination is a file
        if dst_node.is_file:
            # do not overwrite existing file
            raise NodeConflictedError(dst_node)
        # case 2.3 - the distination is a folder
        return await self.rename_node(node, dst_node, None)

    async def sync(self,
        check_point: str = None,
    ) -> AsyncGenerator[ChangeDict, None]:
        async with self._sync_lock:
            dry_run = check_point is not None
            initial_check_point = await self._remote.get_initial_check_point()

            if not dry_run:
                try:
                    check_point = await self._db.get_metadata('check_point')
                except KeyError:
                    check_point = initial_check_point

            # no data before, get the root node and cache it
            if not dry_run and check_point == initial_check_point:
                node = await self._remote.fetch_root_node()
                await self._db.insert_node(node)

            async for next_, changes in self._remote.fetch_changes(check_point):
                changes = await decode_changes(changes, self._context.decode_dict)

                if not dry_run:
                    await self._db.apply_changes(changes, next_)

                for change in changes:
                    yield change

    async def get_hasher(self) -> Hasher:
        fn = self._context.get_hasher(self._remote.get_hasher)
        return await fn()


class DriveFactory(object):

    def __init__(self) -> None:
        self._config_path = get_default_config_path()
        self._data_path = get_default_data_path()
        self._database = None
        self._driver = None
        self._middleware_list = []

    def set_config_path(self, config_path: str) -> None:
        self._config_path = config_path

    def set_data_path(self, data_path: str) -> None:
        self._data_path = data_path

    def set_database(self, dsn: str) -> None:
        self._database = dsn

    def set_driver(self, module_name: str) -> None:
        self._driver = module_name

    def add_middleware(self, middleware_name: str) -> None:
        self._middleware_list.append(middleware_name)

    def load_config(self) -> None:
        config_path = pathlib.Path(self._config_path)
        config_file_path = config_path / 'main.yaml'

        with config_file_path.open('r') as fin:
            config_dict = yaml.safe_load(fin)

        for key in ('version', 'database', 'driver', 'middleware'):
            if key not in config_dict:
                raise ValueError(f'no required key: {key}')

        if not self._database:
            self.set_driver(config_dict['database'])
        if not self._driver:
            self.set_driver(config_dict['driver'])
        if not self._middleware_list:
            self._middleware_list = config_dict['middleware']

    def create_drive(self, pool: concurrent.futures.Executor = None) -> Drive:
        # TODO use real dsn
        path = pathlib.Path(self._database)
        if not path.is_absolute():
            path = pathlib.PurePath(self._data_path) / path
        dsn = str(path)

        ilim = importlib.import_module
        module = ilim(self._driver)
        driver_class = module.Driver
        min_, max_ = driver_class.get_version_range()
        if not min_ <= DRIVER_VERSION <= max_:
            raise InvalidRemoteDriverError()

        middleware_list = []
        for middleware in self._middleware_list:
            module = ilim(middleware)
            middleware_class = module.Middleware
            min_, max_ = middleware_class.get_version_range()
            if not min_ <= DRIVER_VERSION <= max_:
                raise InvalidMiddlewareError()
            middleware = module.Middleware()
            middleware_list.append(middleware)

        context = Context(
            config_path=self._config_path,
            data_path=self._data_path,
            database_dsn=dsn,
            driver_class=driver_class,
            middleware_list=middleware_list,
            pool=pool,
        )
        return Drive(context)


async def decode_changes(changes: List[ChangeDict], decode) -> List[ChangeDict]:
    rv = []
    for change in changes:
        if not change['removed']:
            change['node'] = await decode(change['node'])
        rv.append(change)
    return rv

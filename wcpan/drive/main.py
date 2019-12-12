from typing import List, Optional, AsyncGenerator, Tuple, Any
import argparse
import asyncio
import concurrent.futures
import contextlib
import functools
import io
import os
import pathlib
import sys

from wcpan.logger import setup as setup_logger, EXCEPTION, INFO, ERROR
from wcpan.worker import AsyncQueue
import yaml

from .drive import DriveFactory, Drive
from .types import Node, ChangeDict
from .abc import Hasher
from .util import (
    create_executor,
    get_default_config_path,
    get_default_data_path,
    CHUNK_SIZE,
    download_to_local,
    upload_from_local,
)


class AbstractQueue(object):

    def __init__(self,
        drive: Drive,
        pool: concurrent.futures.Executor,
        jobs: int,
    ) -> None:
        self._drive = drive
        self._queue = AsyncQueue(jobs)
        self._pool = pool
        self._counter = 0
        self._table = {}
        self._total = 0
        self._failed = []

    async def __aenter__(self) -> None:
        return self

    async def __aexit__(self, et, ev, tb) -> bool:
        await self._queue.shutdown()

    @property
    def drive(self) -> Drive:
        return self._drive

    @property
    def failed(self) -> bool:
        return self._failed

    async def run(self,
        src_list: List[pathlib.Path],
        dst: pathlib.Path,
    ) -> None:
        if not src_list:
            return
        self._counter = 0
        self._table = {}
        total = (self.count_tasks(_) for _ in src_list)
        total = await asyncio.gather(*total)
        self._total = sum(total)
        for src in src_list:
            fn = functools.partial(self._run_one_task, src, dst)
            self._queue.post(fn)
        await self._queue.join()

    async def count_tasks(self, src: pathlib.Path) -> int:
        raise NotImplementedError()

    def source_is_folder(self, src: pathlib.Path) -> bool:
        raise NotImplementedError()

    async def do_folder(self,
        src: pathlib.Path,
        dst: pathlib.Path,
    ) -> Optional[pathlib.Path]:
        raise NotImplementedError()

    async def get_children(self, src: pathlib.Path) -> List[pathlib.Path]:
        raise NotImplementedError()

    async def do_file(self,
        src: pathlib.Path,
        dst: pathlib.Path,
    ) -> Optional[pathlib.Path]:
        raise NotImplementedError()

    def get_source_hash(self, src: pathlib.Path) -> str:
        raise NotImplementedError()

    async def get_source_display(self, src: pathlib.Path) -> str:
        raise NotImplementedError()

    async def _run_one_task(self,
        src: pathlib.Path,
        dst: pathlib.Path,
    ) -> Optional[pathlib.Path]:
        self._update_counter_table(src)
        async with self._log_guard(src):
            if self.source_is_folder(src):
                rv = await self._run_for_folder(src, dst)
            else:
                rv = await self._run_for_file(src, dst)
        return rv

    async def _run_for_folder(self,
        src: pathlib.Path,
        dst: pathlib.Path,
    ) -> Optional[pathlib.Path]:
        try:
            rv = await self.do_folder(src, dst)
        except Exception as e:
            EXCEPTION('wcpan.drive', e)
            display = await self.get_source_display(src)
            self._add_failed(display)
            rv = None

        if not rv:
            return None

        children = await self.get_children(src)
        for child in children:
            fn = functools.partial(self._run_one_task, child, rv)
            self._queue.post(fn)

        return rv

    async def _run_for_file(self,
        src: pathlib.Path,
        dst: pathlib.Path,
    ) -> Optional[pathlib.Path]:
        try:
            rv = await self.do_file(src, dst)
        except Exception as e:
            EXCEPTION('wcpan.drive', e)
            display = await self.get_source_display(src)
            self._add_failed(display)
            rv = None
        return rv

    def _add_failed(self, src: str) -> None:
        self._failed.append(src)

    @contextlib.asynccontextmanager
    async def _log_guard(self, src: pathlib.Path) -> AsyncGenerator[None, None]:
        await self._log('begin', src)
        try:
            yield
        finally:
            await self._log('end', src)

    async def _log(self, begin_or_end: str, src: pathlib.Path) -> None:
        progress = self._get_progress(src)
        display = await self.get_source_display(src)
        INFO('wcpan.drive') << f'{progress} {begin_or_end} {display}'

    def _get_progress(self, src: pathlib.Path) -> str:
        key = self.get_source_hash(src)
        id_ = self._table[key]
        return f'[{id_}/{self._total}]'

    def _update_counter_table(self, src: pathlib.Path) -> None:
        key = self.get_source_hash(src)
        self._counter += 1
        self._table[key] = self._counter

    async def _get_hash(self, local_path: pathlib.Path) -> str:
        hasher = await self.drive.get_hasher()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            get_hash,
            local_path,
            hasher,
        )


class UploadQueue(AbstractQueue):

    def __init__(self,
        drive: Drive,
        pool: concurrent.futures.Executor,
        jobs: int,
    ) -> None:
        super(UploadQueue, self).__init__(drive, pool, jobs)

    async def count_tasks(self, local_path: pathlib.Path) -> int:
        total = 1
        for dummy_root, folders, files in os.walk(local_path):
            total = total + len(folders) + len(files)
        return total

    def source_is_folder(self, local_path: pathlib.Path) -> bool:
        return local_path.is_dir()

    async def do_folder(self,
        local_path: pathlib.Path,
        parent_node: Node,
    ) -> Node:
        folder_name = local_path.name
        node = await self.drive.create_folder(
            parent_node,
            folder_name,
            exist_ok=True,
        )
        return node

    async def get_children(self,
        local_path: pathlib.Path,
    ) -> List[pathlib.Path]:
        rv = os.listdir(local_path)
        rv = [local_path / _ for _ in rv]
        return rv

    async def do_file(self,
        local_path: pathlib.Path,
        parent_node: Node,
    ) -> Node:
        node = await upload_from_local(
            self.drive,
            parent_node,
            local_path,
            exist_ok=True,
        )
        local_hash = await self._get_hash(local_path)
        if local_hash != node.hash_:
            raise Exception(f'{local_path} checksum mismatch')
        return node

    def get_source_hash(self, local_path: pathlib.Path) -> str:
        return str(local_path)

    async def get_source_display(self, local_path: pathlib.Path) -> str:
        return str(local_path)


class DownloadQueue(AbstractQueue):

    def __init__(self,
        drive: Drive,
        pool: concurrent.futures.Executor,
        jobs: int,
    ) -> None:
        super(DownloadQueue, self).__init__(drive, pool, jobs)

    async def count_tasks(self, node: Node) -> int:
        total = 1
        children = await self.drive.get_children(node)
        count = (self.count_tasks(_) for _ in children)
        count = await asyncio.gather(*count)
        return total + sum(count)

    def source_is_folder(self, node: Node) -> bool:
        return node.is_folder

    async def do_folder(self,
        node: Node,
        local_path: pathlib.Path,
    ) -> pathlib.Path:
        full_path = local_path / node.name
        os.makedirs(full_path, exist_ok=True)
        return full_path

    async def get_children(self, node: Node) -> List[Node]:
        return await self.drive.get_children(node)

    async def do_file(self,
        node: Node,
        local_path: pathlib.Path,
    ) -> pathlib.Path:
        local_path = await download_to_local(self.drive, node, local_path)
        local_hash = await self._get_hash(local_path)
        if local_hash != node.hash_:
            raise Exception(f'{local_path} checksum mismatch')
        return local_path

    def get_source_hash(self, node: Node) -> str:
        return node.id_

    async def get_source_display(self, node: Node) -> str:
        return await self.drive.get_path(node)


class UploadVerifier(object):

    def __init__(self, drive: Drive, pool: concurrent.futures.Executor) -> None:
        self._drive = drive
        self._pool = pool

    async def run(self, local_path: pathlib.Path, remote_node: Node) -> None:
        if local_path.is_dir():
            await self._run_folder(local_path, remote_node)
        else:
            await self._run_file(local_path, remote_node)

    async def _run_folder(self,
        local_path: pathlib.Path,
        remote_node: Node,
    ) -> None:
        dir_name = local_path.name

        child_node = await self._get_child_node(
            local_path,
            dir_name,
            remote_node,
        )
        if not child_node:
            return
        if not child_node.is_folder:
            ERROR('wcpan.drive') << f'[NOT_FOLDER] {local_path}'
            return

        INFO('wcpan.drive') << f'[OK] {local_path}'

        children = [self.run(child_path, child_node)
                    for child_path in local_path.iterdir()]
        if children:
            await asyncio.wait(children)

    async def _run_file(self,
        local_path: pathlib.Path,
        remote_node: Node,
    ) -> None:
        file_name = local_path.name
        remote_path = await self._drive.get_path(remote_node)
        remote_path = pathlib.Path(remote_path, file_name)

        child_node = await self._get_child_node(
            local_path,
            file_name,
            remote_node,
        )
        if not child_node:
            return
        if not child_node.is_file:
            ERROR('wcpan.drive') << f'[NOT_FILE] {local_path}'
            return

        local_hash = await self._get_hash(local_path)
        if local_hash != child_node.hash_:
            ERROR('wcpan.drive') << f'[WRONG_HASH] {local_path}'
            return

        INFO('wcpan.drive') << f'[OK] {local_path}'

    async def _get_child_node(self,
        local_path: pathlib.Path,
        name: str,
        remote_node: Node,
    ) -> Node:
        child_node = await self._drive.get_node_by_name_from_parent(
            name,
            remote_node,
        )
        if not child_node:
            ERROR('wcpan.drive') << f'[MISSING] {local_path}'
            return None
        if child_node.trashed:
            ERROR('wcpan.drive') << f'[TRASHED] {local_path}'
            return None
        return child_node

    async def _get_hash(self, local_path: pathlib.Path) -> str:
        hasher = await self._drive.get_hasher()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            get_hash,
            local_path,
            hasher,
        )


async def main(args: List[str] = None) -> int:
    if args is None:
        args = sys.argv

    setup_logger((
        'wcpan.drive',
    ))

    args = parse_args(args[1:])
    if not args.action:
        await args.fallback_action()
        return 0

    factory = DriveFactory()
    factory.set_config_path(args.config_prefix)
    factory.set_data_path(args.data_prefix)

    with create_executor() as pool:
        async with factory.create_drive(pool) as drive:
            return await args.action(drive, pool, args)


def parse_args(args: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser('wcpan.drive')

    parser.add_argument('--config-prefix',
        default=get_default_config_path(),
        help=(
            'specify configuration file path'
            ' (default: %(default)s)'
        ),
    )
    parser.add_argument('--data-prefix',
        default=get_default_data_path(),
        help=(
            'specify data file path'
            ' (default: %(default)s)'
        ),
    )

    commands = parser.add_subparsers()

    sync_parser = commands.add_parser('sync', aliases=['s'],
        help='synchronize database',
    )
    add_bool_argument(sync_parser, 'verbose', 'v')
    sync_parser.add_argument('-f', '--from', type=int, dest='from_',
        default=None,
        help=(
            'synchronize from certain check point, and do not update cache'
            ' (default: %(default)s)'
        ),
    )
    sync_parser.set_defaults(action=action_sync)

    find_parser = commands.add_parser('find', aliases=['f'],
        help='find files/folders by pattern [offline]',
    )
    add_bool_argument(find_parser, 'id_only')
    add_bool_argument(find_parser, 'include_trash')
    find_parser.add_argument('pattern', type=str)
    find_parser.set_defaults(action=action_find, id_only=False,
                             include_trash=False)

    list_parser = commands.add_parser('list', aliases=['ls'],
        help='list folder [offline]',
    )
    list_parser.set_defaults(action=action_list)
    list_parser.add_argument('id_or_path', type=str)

    tree_parser = commands.add_parser('tree',
        help='recursive list folder [offline]',
    )
    tree_parser.set_defaults(action=action_tree)
    tree_parser.add_argument('id_or_path', type=str)

    dl_parser = commands.add_parser('download', aliases=['dl'],
        help='download files/folders',
    )
    dl_parser.set_defaults(action=action_download)
    dl_parser.add_argument('-j', '--jobs', type=int,
        default=1,
        help=(
            'maximum simultaneously download jobs'
            ' (default: %(default)s)'
        ),
    )
    dl_parser.add_argument('id_or_path', type=str, nargs='+')
    dl_parser.add_argument('destination', type=str)

    ul_parser = commands.add_parser('upload', aliases=['ul'],
        help='upload files/folders',
    )
    ul_parser.set_defaults(action=action_upload)
    ul_parser.add_argument('-j', '--jobs', type=int,
        default=1,
        help=(
            'maximum simultaneously upload jobs'
            ' (default: %(default)s)'
        ),
    )
    ul_parser.add_argument('source', type=str, nargs='+')
    ul_parser.add_argument('id_or_path', type=str)

    rm_parser = commands.add_parser('remove', aliases=['rm'],
        help='trash files/folders',
    )
    rm_parser.set_defaults(action=action_remove)
    rm_parser.add_argument('id_or_path', type=str, nargs='+')

    mv_parser = commands.add_parser('rename', aliases=['mv'],
        help='rename file/folder',
    )
    mv_parser.set_defaults(action=action_rename)
    mv_parser.add_argument('source_id_or_path', type=str)
    mv_parser.add_argument('destination_path', type=str)

    v_parser = commands.add_parser('verify', aliases=['v'],
        help='verify uploaded files/folders',
    )
    v_parser.set_defaults(action=action_verify)
    v_parser.add_argument('source', type=str, nargs='+')
    v_parser.add_argument('id_or_path', type=str)

    d_parser = commands.add_parser('doctor',
        help='check file system error'
    )
    d_parser.set_defaults(action=action_doctor)

    sout = io.StringIO()
    parser.print_help(sout)
    fallback = functools.partial(action_help, sout.getvalue())
    parser.set_defaults(action=None, fallback_action=fallback)

    args = parser.parse_args(args)

    return args


def add_bool_argument(
    parser: argparse.ArgumentParser,
    name: str,
    short_name: str = None,
) -> None:
    flag = name.replace('_', '-')
    pos_flags = ['--' + flag]
    if short_name:
        pos_flags.append('-' + short_name)
    neg_flag = '--no-' + flag
    parser.add_argument(*pos_flags, dest=name, action='store_true')
    parser.add_argument(neg_flag, dest=name, action='store_false')


async def action_help(message: str) -> None:
    print(message)


async def action_sync(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    chunks = chunks_of(drive.sync(check_point=args.from_), 100)
    async for changes in chunks:
        if not args.verbose:
            print(len(changes))
        else:
            for change in changes:
                print_as_yaml(change)
    return 0


async def action_find(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    nodes = await drive.find_nodes_by_regex(args.pattern)
    if not args.include_trash:
        nodes = (_ for _ in nodes if not _.trashed)
    nodes = (wait_for_value(_.id_, drive.get_path(_)) for _ in nodes)
    nodes = await asyncio.gather(*nodes)
    nodes = dict(nodes)

    if args.id_only:
        for id_ in nodes:
            print(id_)
    else:
        print_id_node_dict(nodes)

    return 0


async def action_list(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node = await get_node_by_id_or_path(drive, args.id_or_path)
    nodes = await drive.get_children(node)
    nodes = {_.id_: _.name for _ in nodes}
    print_id_node_dict(nodes)
    return 0


async def action_tree(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node = await get_node_by_id_or_path(drive, args.id_or_path)
    await traverse_node(drive, node, 0)
    return 0


async def action_download(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node_list = (get_node_by_id_or_path(drive, _) for _ in args.id_or_path)
    node_list = await asyncio.gather(*node_list)
    node_list = [_ for _ in node_list if not _.trashed]

    async with DownloadQueue(drive, pool, args.jobs) as queue_:
        dst = pathlib.Path(args.destination)
        await queue_.run(node_list, dst)

    if not queue_.failed:
        return 0
    print('download failed:')
    print_as_yaml(queue_.failed)
    return 1


async def action_upload(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node = await get_node_by_id_or_path(drive, args.id_or_path)

    async with UploadQueue(drive, pool, args.jobs) as queue_:
        src = pathlib.Path(args.source)
        await queue_.run(src, node)

    if not queue_.failed:
        return 0
    print('upload failed:')
    print_as_yaml(queue_.failed)
    return 1


async def action_remove(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    rv = (trash_node(drive, _) for _ in args.id_or_path)
    rv = await asyncio.gather(*rv)
    rv = filter(None, rv)
    rv = list(rv)
    if not rv:
        return 0
    print('trash failed:')
    print_as_yaml(rv)
    return 1


async def action_rename(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node = await get_node_by_id_or_path(drive, args.source_id_or_path)
    node = await drive.rename_node(node, args.destination_path)
    path = await drive.get_path(node)
    return 0 if path else 1


async def action_verify(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    node = await get_node_by_id_or_path(drive, args.id_or_path)

    v = UploadVerifier(drive, pool)
    tasks = (pathlib.Path(local_path) for local_path in args.source)
    tasks = [v.run(local_path, node) for local_path in tasks]
    await asyncio.wait(tasks)

    return 0


async def action_doctor(
    drive: Drive,
    pool: concurrent.futures.Executor,
    args: argparse.Namespace,
) -> int:
    for node in await drive.find_multiple_parents_nodes():
        print(f'{node.name} has multiple parents, please select one parent:')
        parent_list = (drive.get_node_by_id(_) for _ in node.parent_list)
        parent_list = await asyncio.gather(*parent_list)
        for index, parent_node in enumerate(parent_list):
            print(f'{index}: {parent_node.name}')
        try:
            choice = input()
            choice = int(choice)
            parent = parent_list[choice]
            await drive.set_node_parent_by_id(node, parent.id_)
        except Exception as e:
            print('unknown error, skipped', e)
            continue


async def get_node_by_id_or_path(drive: Drive, id_or_path: str) -> Node:
    if id_or_path[0] == '/':
        node = await drive.get_node_by_path(id_or_path)
    else:
        node = await drive.get_node_by_id(id_or_path)
    return node


async def traverse_node(drive: Drive, node: Node, level: int) -> None:
    if node.is_root:
        print_node('/', level)
    elif level == 0:
        top_path = await drive.get_path(node)
        print_node(top_path, level)
    else:
        print_node(node.name, level)

    if node.is_folder:
        children = await drive.get_children_by_id(node.id_)
        for child in children:
            await traverse_node(drive, child, level + 1)


async def trash_node(drive: Drive, id_or_path: str) -> Optional[str]:
    '''
    :returns: None if succeed, id_or_path if failed
    '''
    node = await get_node_by_id_or_path(drive, id_or_path)
    if not node:
        return id_or_path
    try:
        await drive.trash_node(node)
    except Exception as e:
        EXCEPTION('wcpan.drive', e) << 'trash failed'
        return id_or_path
    return None


async def wait_for_value(k, v) -> Tuple[str, Any]:
    return k, await v


def get_hash(local_path: pathlib.Path, hasher: Hasher) -> str:
    with open(local_path, 'rb') as fin:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


async def chunks_of(
    ag: AsyncGenerator[ChangeDict, None],
    size: int,
) -> AsyncGenerator[List[ChangeDict], None]:
    chunk = []
    async for item in ag:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def print_node(name: str, level: int) -> None:
    level = ' ' * level
    print(level + name)


def print_as_yaml(data: Any) -> None:
    yaml.safe_dump(
        data,
        stream=sys.stdout,
        allow_unicode=True,
        encoding=sys.stdout.encoding,
        default_flow_style=False,
    )


def print_id_node_dict(data: Any) -> None:
    pairs = sorted(data.items(), key=lambda _: _[1])
    for id_, path in pairs:
        print(f'{id_}: {path}')

import asyncio
import enum
import pathlib
import shlex
from collections.abc import Iterator

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from wcpan.drive.client.exceptions import NodeNotFoundError
from wcpan.drive.client.types import Drive, Node

from ._lib import cout, print_as_yaml


class TokenType(enum.Enum):
    Global = enum.auto()
    Path = enum.auto()


class DriveCompleter(Completer):
    def __init__(self, context: "ShellContext") -> None:
        self._context = context

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        return iter(())

    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ):
        text = document.text_before_cursor
        end_index = len(text)
        type_, token = parse_completion(text, end_index)
        if token is None:
            token = ""

        if type_ == TokenType.Global:
            for cmd in self._context.get_commands():
                if cmd.startswith(token):
                    yield Completion(cmd, start_position=-len(token))
        elif type_ == TokenType.Path:
            prefix = pathlib.PurePath(token).name if token else ""
            async for name in self._context.iter_path_completions(token):
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix))


class ShellContext:
    def __init__(self, drive: Drive, home_node: Node) -> None:
        self._drive = drive
        self._home = home_node
        self._cwd = home_node
        self._actions = {
            "help": self._help,
            "ls": self._list,
            "cd": self._chdir,
            "mkdir": self._mkdir,
            "sync": self._sync,
            "pwd": self._pwd,
            "find": self._find,
            "info": self._info,
            "hash": self._hash,
            "id_to_path": self._id_to_path,
            "path_to_id": self._path_to_id,
        }

    def get_prompt(self) -> str:
        if not self._cwd.name:
            name = "/"
        else:
            name = self._cwd.name
        return f"{name} > "

    def get_commands(self) -> list[str]:
        return list(self._actions.keys())

    async def iter_path_completions(self, token: str):
        path = await normalize_path(self._drive, self._cwd, token)
        try:
            node = await self._drive.get_node_by_path(path)
        except NodeNotFoundError:
            try:
                node = await self._drive.get_node_by_path(path.parent)
            except NodeNotFoundError:
                return
        children = await self._drive.get_children(node)
        for child in children:
            yield child.name

    async def execute(self, line: str) -> None:
        cmd = shlex.split(line)

        if not cmd:
            return

        command = cmd[0]
        if command not in self._actions:
            cout(f"unknown command {command}")
            return

        action = self._actions[command]
        try:
            await action(*cmd[1:])
        except TypeError as e:
            cout(e)

    async def _help(self) -> None:
        for c in self._actions.keys():
            cout(c)

    async def _list(self, src: str | None = None) -> None:
        if not src:
            node = self._cwd
        else:
            path = await normalize_path(self._drive, self._cwd, src)
            try:
                node = await self._drive.get_node_by_path(path)
            except NodeNotFoundError:
                cout(f"{src} not found")
                return

        children = await self._drive.get_children(node)
        for child in children:
            cout(child.name)

    async def _chdir(self, src: str | None = None) -> None:
        if not src:
            self._cwd = self._home
            return

        path = await normalize_path(self._drive, self._cwd, src)
        try:
            node = await self._drive.get_node_by_path(path)
        except NodeNotFoundError:
            cout(f"unknown path {src}")
            return
        if not node.is_directory:
            cout(f"{src} is not a folder")
            return

        self._cwd = node

    async def _mkdir(self, src: str) -> None:
        if not src:
            cout("invalid name")
            return
        path = await normalize_path(self._drive, self._cwd, src)
        try:
            parent = await self._drive.get_node_by_path(path.parent)
        except NodeNotFoundError:
            cout(f"{path.parent} not found")
            return
        await self._drive.create_directory(path.name, parent)

    async def _sync(self) -> None:
        async for change in self._drive.sync():
            cout(change)

    async def _pwd(self) -> None:
        path = await self._drive.resolve_path(self._cwd)
        cout(path)

    async def _find(self, src: str) -> None:
        node_list = await self._drive.find_nodes_by_regex(src)
        path_list = await asyncio.gather(
            *[self._drive.resolve_path(n) for n in node_list]
        )
        for node, path in zip(node_list, path_list):
            cout(f"{node.id} - {path}")

    async def _info(self, src: str) -> None:
        from dataclasses import asdict

        base_path = await self._drive.resolve_path(self._cwd)
        try:
            node = await get_node_by_path_or_id(self._drive, base_path, src)
        except NodeNotFoundError:
            cout("null")
            return
        print_as_yaml(asdict(node))

    async def _hash(self, *args: str) -> None:
        base_path = await self._drive.resolve_path(self._cwd)
        node_list = await asyncio.gather(
            *[get_node_by_path_or_id(self._drive, base_path, p) for p in args]
        )
        for path_or_id, node in zip(args, node_list):
            cout(f"{node.hash} - {path_or_id}")

    async def _id_to_path(self, src: str) -> None:
        try:
            node = await self._drive.get_node_by_id(src)
        except NodeNotFoundError:
            cout(f"{src} not found")
            return
        path = await self._drive.resolve_path(node)
        cout(path)

    async def _path_to_id(self, src: str) -> None:
        path = await normalize_path(self._drive, self._cwd, src)
        try:
            node = await self._drive.get_node_by_path(path)
        except NodeNotFoundError:
            cout(f"{src} not found")
            return
        cout(node.id)


async def interact(drive: Drive, home_node: Node) -> None:
    context = ShellContext(drive, home_node)
    completer = DriveCompleter(context)
    session: PromptSession[str] = PromptSession(completer=completer)

    while True:
        prompt = context.get_prompt()
        try:
            line = await session.prompt_async(prompt)
        except EOFError:
            break
        except KeyboardInterrupt:
            continue

        await context.execute(line)

    # reset anchor
    cout()


def resolve_path(
    from_: pathlib.PurePath,
    to: pathlib.PurePath,
) -> pathlib.PurePath:
    rv = from_
    for part in to.parts:
        if part == ".":
            continue
        elif part == "..":
            rv = rv.parent
        else:
            rv = rv / part
    return rv


async def normalize_path(
    drive: Drive,
    node: Node,
    string: str,
) -> pathlib.PurePath:
    path = pathlib.PurePath(string)
    if not path.is_absolute():
        current_path = await drive.resolve_path(node)
        path = resolve_path(current_path, path)
    return path


def parse_completion(whole_text: str, end_index: int) -> tuple[TokenType, str | None]:
    lexer = shlex.shlex(whole_text, posix=True)
    lexer.whitespace_split = True

    cmd: list[tuple[int, str]] = []
    offset = 0

    while True:
        try:
            token = lexer.get_token()
        except ValueError:
            token = lexer.token
            idx = whole_text.find(token, offset)
            assert idx >= 0
            offset = idx + len(token)
            cmd.append((idx, lexer.token))
            break

        if token is None or token == lexer.eof:
            break

        idx = whole_text.find(token, offset)
        assert idx >= 0
        offset = idx + len(token)
        cmd.append((idx, token))

    idx = None
    token = None
    token_list = [(idx, offset, token) for idx, (offset, token) in enumerate(cmd)]
    for idx, offset, token in reversed(token_list):
        if offset <= end_index:
            break

    if idx == 0:
        return TokenType.Global, token
    else:
        return TokenType.Path, token


async def get_node_by_path_or_id(
    drive: Drive,
    cwd: pathlib.PurePath,
    path_or_id: str,
) -> Node:
    try:
        return await drive.get_node_by_id(path_or_id)
    except NodeNotFoundError:
        pass

    path = pathlib.PurePath(path_or_id)
    if not path.is_absolute():
        path = resolve_path(cwd, path)
    return await drive.get_node_by_path(path)

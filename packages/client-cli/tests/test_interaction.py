from dataclasses import replace
from pathlib import PurePath
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from wcpan.drive.client.cli._interaction import (
    DriveCompleter,
    ShellContext,
    TokenType,
    get_node_by_path_or_id,
    normalize_path,
    parse_completion,
    resolve_path,
)
from wcpan.drive.client.exceptions import NodeNotFoundError
from wcpan.drive.client.types import Drive, NodeQuery

from ._lib import aexpect, bind_find_nodes, create_amock, make_node


class TestResolvePath(TestCase):
    def test_absolute_path_ignores_cwd(self):
        result = resolve_path(PurePath("/a/b"), PurePath("/x/y"))
        self.assertEqual(result, PurePath("/x/y"))

    def test_dot_returns_cwd(self):
        result = resolve_path(PurePath("/a/b"), PurePath("."))
        self.assertEqual(result, PurePath("/a/b"))

    def test_dotdot_goes_up(self):
        result = resolve_path(PurePath("/a/b/c"), PurePath(".."))
        self.assertEqual(result, PurePath("/a/b"))

    def test_relative_name_appends(self):
        result = resolve_path(PurePath("/a/b"), PurePath("c"))
        self.assertEqual(result, PurePath("/a/b/c"))

    def test_multi_segment_relative(self):
        result = resolve_path(PurePath("/a"), PurePath("b/c"))
        self.assertEqual(result, PurePath("/a/b/c"))


class TestParseCompletion(TestCase):
    def test_empty_input_returns_path_none(self):
        type_, token = parse_completion("", 0)
        self.assertEqual(type_, TokenType.Path)
        self.assertIsNone(token)

    def test_unclosed_first_quote(self):
        text = "'partial"
        type_, token = parse_completion(text, len(text))
        self.assertEqual(type_, TokenType.Global)
        self.assertEqual(token, "partial")

    def test_command_token(self):
        text = "ls"
        type_, token = parse_completion(text, len(text))
        self.assertEqual(type_, TokenType.Global)
        self.assertEqual(token, "ls")

    def test_path_token_after_command(self):
        text = "ls /foo"
        type_, token = parse_completion(text, len(text))
        self.assertEqual(type_, TokenType.Path)
        self.assertEqual(token, "/foo")

    def test_quoted_path_token(self):
        text = "cd '/foo bar'"
        type_, token = parse_completion(text, len(text))
        self.assertEqual(type_, TokenType.Path)
        self.assertEqual(token, "/foo bar")


class TestShellContextGetPrompt(TestCase):
    def test_root_node_shows_slash(self):
        drive = create_amock(Drive)
        home = make_node(name="", is_directory=True, parent_id=None)
        ctx = ShellContext(drive, home)
        self.assertEqual(ctx.get_prompt(), "/ > ")

    def test_named_node_shows_name(self):
        drive = create_amock(Drive)
        home = make_node(name="mydir", is_directory=True)
        ctx = ShellContext(drive, home)
        self.assertEqual(ctx.get_prompt(), "mydir > ")


class TestShellContextGetCommands(TestCase):
    def test_returns_all_commands(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        commands = ctx.get_commands()
        for cmd in ("help", "ls", "cd", "mkdir", "sync", "pwd", "find", "info"):
            self.assertIn(cmd, commands)


class TestShellContextExecute(IsolatedAsyncioTestCase):
    async def test_empty_line_is_noop(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        await ctx.execute("")
        aexpect(drive.get_children).assert_not_called()

    async def test_unknown_command_prints_message(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("foo")
        mock_cout.assert_called_once_with("unknown command foo")

    async def test_too_many_args_prints_type_error(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("mkdir a b")
        mock_cout.assert_called_once()


class TestShellContextHelp(IsolatedAsyncioTestCase):
    async def test_prints_all_commands(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("help")
        printed = [call.args[0] for call in mock_cout.call_args_list]
        for cmd in ("help", "ls", "cd", "mkdir", "sync", "pwd", "find", "info"):
            self.assertIn(cmd, printed)


class TestShellContextChdir(IsolatedAsyncioTestCase):
    async def test_no_arg_resets_to_home(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        other = make_node(id="other-id", name="other", is_directory=True)
        ctx = ShellContext(drive, home)
        ctx._cwd = other
        await ctx.execute("cd")
        self.assertIs(ctx._cwd, home)

    async def test_valid_directory_changes_cwd(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        aexpect(drive.get_node_by_path).return_value = target
        ctx = ShellContext(drive, home)
        await ctx.execute("cd /docs")
        self.assertIs(ctx._cwd, target)

    async def test_nonexistent_path_prints_message(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.get_node_by_path).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("cd /no/such/path")
        mock_cout.assert_called_once_with("unknown path /no/such/path")

    async def test_file_not_folder_prints_message(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        file_node = make_node(id="file-id", name="readme.txt", is_directory=False)
        aexpect(drive.get_node_by_path).return_value = file_node
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("cd /readme.txt")
        mock_cout.assert_called_once_with("/readme.txt is not a folder")


class TestShellContextList(IsolatedAsyncioTestCase):
    async def test_no_arg_lists_cwd_children(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        child = make_node(id="child-id", name="file.txt")
        aexpect(drive.get_children).return_value = [child]
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("ls")
        aexpect(drive.get_children).assert_called_once_with(home)
        mock_cout.assert_called_once_with("file.txt")

    async def test_with_path_lists_target_children(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        child = make_node(id="child-id", name="doc.txt")
        aexpect(drive.get_node_by_path).return_value = target
        aexpect(drive.get_children).return_value = [child]
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("ls /docs")
        mock_cout.assert_called_once_with("doc.txt")


class TestShellContextPwd(IsolatedAsyncioTestCase):
    async def test_prints_resolved_path(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("pwd")
        aexpect(drive.resolve_path).assert_called_once_with(home)
        mock_cout.assert_called_once_with(PurePath("/home"))


class TestShellContextMkdir(IsolatedAsyncioTestCase):
    async def test_creates_directory(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_path).return_value = home
        ctx = ShellContext(drive, home)
        await ctx.execute("mkdir newdir")
        aexpect(drive.create_directory).assert_called_once_with("newdir", home)

    async def test_relative_path_creates_under_resolved_parent(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        parent = make_node(id="parent-id", name="subdir", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_path).return_value = parent
        ctx = ShellContext(drive, home)
        await ctx.execute("mkdir subdir/newdir")
        aexpect(drive.create_directory).assert_called_once_with("newdir", parent)

    async def test_nonexistent_parent_prints_error(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_path).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("mkdir subdir/newdir")
        mock_cout.assert_called_once()


class TestShellContextSync(IsolatedAsyncioTestCase):
    async def test_prints_each_change(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)

        async def fake_sync():
            yield "change1"
            yield "change2"

        aexpect(drive.sync).return_value = fake_sync()
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("sync")
        self.assertEqual(mock_cout.call_count, 2)


class TestShellContextFind(IsolatedAsyncioTestCase):
    async def test_prints_id_and_path_for_each_result(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        node1 = make_node(id="id1", name="file1.txt")
        node2 = make_node(id="id2", name="file2.txt")
        query_nodes = bind_find_nodes(drive)
        query_nodes.return_value = [node1, node2]
        aexpect(drive.resolve_path).side_effect = [
            PurePath("/a/file1.txt"),
            PurePath("/a/file2.txt"),
        ]
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("find pattern")
        printed = [call.args[0] for call in mock_cout.call_args_list]
        self.assertIn("id1 - /a/file1.txt", printed)
        self.assertIn("id2 - /a/file2.txt", printed)
        query_nodes.assert_awaited_once_with(
            NodeQuery(name_pattern="pattern", is_trashed=None)
        )


class TestShellContextInfo(IsolatedAsyncioTestCase):
    async def test_with_id_prints_yaml(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_id).return_value = target
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.print_as_yaml") as mock_yaml:
            await ctx.execute("info file-id")
        mock_yaml.assert_called_once()

    async def test_with_path_prints_yaml(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        aexpect(drive.get_node_by_path).return_value = target
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.print_as_yaml") as mock_yaml:
            await ctx.execute("info /file.txt")
        mock_yaml.assert_called_once()

    async def test_not_found_prints_null(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        aexpect(drive.get_node_by_path).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("info /no/such")
        mock_cout.assert_called_once_with("null")


class TestShellContextHash(IsolatedAsyncioTestCase):
    async def test_prints_hash_and_arg(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        target = replace(
            make_node(name="file.txt"), hash="abc123", hash_algorithm="md5"
        )
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        aexpect(drive.get_node_by_path).return_value = target
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("hash /file.txt")
        mock_cout.assert_called_once_with("abc123 - /file.txt")


class TestShellContextIdToPath(IsolatedAsyncioTestCase):
    async def test_prints_path_for_id(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.get_node_by_id).return_value = target
        aexpect(drive.resolve_path).return_value = PurePath("/path/to/file.txt")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("id_to_path file-id")
        mock_cout.assert_called_once_with(PurePath("/path/to/file.txt"))

    async def test_not_found_prints_message(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("id_to_path missing-id")
        mock_cout.assert_called_once_with("missing-id not found")


class TestShellContextPathToId(IsolatedAsyncioTestCase):
    async def test_prints_id_for_path(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.get_node_by_path).return_value = target
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("path_to_id /file.txt")
        mock_cout.assert_called_once_with("file-id")

    async def test_not_found_prints_message(self):
        drive = create_amock(Drive)
        home = make_node(is_directory=True)
        aexpect(drive.get_node_by_path).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("path_to_id /no/such")
        mock_cout.assert_called_once_with("/no/such not found")


class TestNormalizePath(IsolatedAsyncioTestCase):
    async def test_absolute_path_returned_as_is(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        result = await normalize_path(drive, home, "/absolute/path")
        self.assertEqual(result, PurePath("/absolute/path"))
        aexpect(drive.resolve_path).assert_not_called()

    async def test_relative_path_resolved_against_cwd(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        result = await normalize_path(drive, home, "relative")
        self.assertEqual(result, PurePath("/home/relative"))
        aexpect(drive.resolve_path).assert_called_once_with(home)


class TestGetNodeByPathOrId(IsolatedAsyncioTestCase):
    async def test_finds_by_id(self):
        drive = create_amock(Drive)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.get_node_by_id).return_value = target
        result = await get_node_by_path_or_id(drive, PurePath("/home"), "file-id")
        self.assertIs(result, target)
        aexpect(drive.get_node_by_path).assert_not_called()

    async def test_falls_through_to_absolute_path(self):
        drive = create_amock(Drive)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        aexpect(drive.get_node_by_path).return_value = target
        result = await get_node_by_path_or_id(
            drive, PurePath("/home"), "/absolute/path"
        )
        self.assertIs(result, target)
        aexpect(drive.get_node_by_path).assert_called_once_with(
            PurePath("/absolute/path")
        )

    async def test_falls_through_to_relative_path(self):
        drive = create_amock(Drive)
        target = make_node(id="file-id", name="file.txt")
        aexpect(drive.get_node_by_id).side_effect = NodeNotFoundError("not found")
        aexpect(drive.get_node_by_path).return_value = target
        result = await get_node_by_path_or_id(drive, PurePath("/home"), "relative")
        self.assertIs(result, target)
        aexpect(drive.get_node_by_path).assert_called_once_with(
            PurePath("/home/relative")
        )


class TestShellContextIterPathCompletions(IsolatedAsyncioTestCase):
    async def test_existing_path_yields_children(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        child1 = make_node(id="c1", name="file1.txt")
        child2 = make_node(id="c2", name="file2.txt")
        aexpect(drive.get_node_by_path).return_value = target
        aexpect(drive.get_children).return_value = [child1, child2]
        ctx = ShellContext(drive, home)
        names = [name async for name in ctx.iter_path_completions("/docs")]
        self.assertIn("file1.txt", names)
        self.assertIn("file2.txt", names)

    async def test_incomplete_path_falls_back_to_parent(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        parent = make_node(id="parent-id", name="docs", is_directory=True)
        child = make_node(id="c1", name="file1.txt")
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_path).side_effect = [
            NodeNotFoundError("not found"),
            parent,
        ]
        aexpect(drive.get_children).return_value = [child]
        ctx = ShellContext(drive, home)
        names = [name async for name in ctx.iter_path_completions("partial")]
        self.assertIn("file1.txt", names)

    async def test_completely_missing_path_yields_nothing(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        aexpect(drive.resolve_path).return_value = PurePath("/home")
        aexpect(drive.get_node_by_path).side_effect = NodeNotFoundError("not found")
        ctx = ShellContext(drive, home)
        names = [name async for name in ctx.iter_path_completions("missing")]
        self.assertEqual(names, [])


class TestDriveCompleter(IsolatedAsyncioTestCase):
    async def test_partial_command_yields_matching_completions(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)
        doc = Document("l", 1)
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        names = [c.text for c in completions]
        self.assertIn("ls", names)
        for name in names:
            self.assertTrue(name.startswith("l"))

    async def test_nonmatching_prefix_yields_nothing(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)
        doc = Document("zzz", 3)
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        self.assertEqual(completions, [])

    async def test_path_argument_yields_path_completions(self):
        drive = create_amock(Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)

        async def fake_iter(token: str):
            yield "file1.txt"
            yield "file2.txt"

        ctx.iter_path_completions = fake_iter
        doc = Document("ls /file", 8)
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        names = [c.text for c in completions]
        self.assertIn("file1.txt", names)
        self.assertIn("file2.txt", names)

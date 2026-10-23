from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import PurePath
from unittest import TestCase
from unittest.mock import patch

from decoy import matchers
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
from wcpan.drive.client.types import ChangeAction, Drive, NodeQuery

from ._lib import DecoyTestCase, bind_find_nodes, make_node


class TestResolvePath(TestCase):
    def test_absolute_path_ignores_cwd(self):
        # when
        result = resolve_path(PurePath("/a/b"), PurePath("/x/y"))
        # then
        self.assertEqual(result, PurePath("/x/y"))

    def test_dot_returns_cwd(self):
        # when
        result = resolve_path(PurePath("/a/b"), PurePath("."))
        # then
        self.assertEqual(result, PurePath("/a/b"))

    def test_dotdot_goes_up(self):
        # when
        result = resolve_path(PurePath("/a/b/c"), PurePath(".."))
        # then
        self.assertEqual(result, PurePath("/a/b"))

    def test_relative_name_appends(self):
        # when
        result = resolve_path(PurePath("/a/b"), PurePath("c"))
        # then
        self.assertEqual(result, PurePath("/a/b/c"))

    def test_multi_segment_relative(self):
        # when
        result = resolve_path(PurePath("/a"), PurePath("b/c"))
        # then
        self.assertEqual(result, PurePath("/a/b/c"))


class TestParseCompletion(TestCase):
    def test_empty_input_returns_path_none(self):
        # when
        type_, token = parse_completion("", 0)
        # then
        self.assertEqual(type_, TokenType.Path)
        self.assertIsNone(token)

    def test_unclosed_first_quote(self):
        # given
        text = "'partial"
        # when
        type_, token = parse_completion(text, len(text))
        # then
        self.assertEqual(type_, TokenType.Global)
        self.assertEqual(token, "partial")

    def test_command_token(self):
        # given
        text = "ls"
        # when
        type_, token = parse_completion(text, len(text))
        # then
        self.assertEqual(type_, TokenType.Global)
        self.assertEqual(token, "ls")

    def test_path_token_after_command(self):
        # given
        text = "ls /foo"
        # when
        type_, token = parse_completion(text, len(text))
        # then
        self.assertEqual(type_, TokenType.Path)
        self.assertEqual(token, "/foo")

    def test_quoted_path_token(self):
        # given
        text = "cd '/foo bar'"
        # when
        type_, token = parse_completion(text, len(text))
        # then
        self.assertEqual(type_, TokenType.Path)
        self.assertEqual(token, "/foo bar")


class TestShellContextGetPrompt(DecoyTestCase):
    def test_root_node_shows_slash(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="", is_directory=True, parent_id=None)
        ctx = ShellContext(drive, home)
        # when / then
        self.assertEqual(ctx.get_prompt(), "/ > ")

    def test_named_node_shows_name(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="mydir", is_directory=True)
        ctx = ShellContext(drive, home)
        # when / then
        self.assertEqual(ctx.get_prompt(), "mydir > ")


class TestShellContextGetCommands(DecoyTestCase):
    def test_returns_all_commands(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        # when
        commands = ctx.get_commands()
        # then
        for cmd in ("help", "ls", "cd", "mkdir", "sync", "pwd", "find", "info"):
            self.assertIn(cmd, commands)


class TestShellContextExecute(DecoyTestCase):
    async def test_empty_line_is_noop(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        # when
        await ctx.execute("")
        # then
        self.decoy.verify(await drive.get_children(matchers.Anything()), times=0)

    async def test_unknown_command_prints_message(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("foo")
        # then
        mock_cout.assert_called_once_with("unknown command foo")

    async def test_too_many_args_prints_type_error(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("mkdir a b")
        # then
        mock_cout.assert_called_once()


class TestShellContextHelp(DecoyTestCase):
    async def test_prints_all_commands(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("help")
        # then
        printed = [call.args[0] for call in mock_cout.call_args_list]
        for cmd in ("help", "ls", "cd", "mkdir", "sync", "pwd", "find", "info"):
            self.assertIn(cmd, printed)


class TestShellContextChdir(DecoyTestCase):
    async def test_no_arg_resets_to_home(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        other = make_node(id="other-id", name="other", is_directory=True)
        ctx = ShellContext(drive, home)
        ctx._cwd = other
        # when
        await ctx.execute("cd")
        # then
        self.assertIs(ctx._cwd, home)

    async def test_valid_directory_changes_cwd(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        self.decoy.when(await drive.get_node_by_path(PurePath("/docs"))).then_return(
            target
        )
        ctx = ShellContext(drive, home)
        # when
        await ctx.execute("cd /docs")
        # then
        self.assertIs(ctx._cwd, target)

    async def test_nonexistent_path_prints_message(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("cd /no/such/path")
        # then
        mock_cout.assert_called_once_with("unknown path /no/such/path")

    async def test_file_not_folder_prints_message(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        file_node = make_node(id="file-id", name="readme.txt", is_directory=False)
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/readme.txt"))
        ).then_return(file_node)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("cd /readme.txt")
        # then
        mock_cout.assert_called_once_with("/readme.txt is not a folder")


class TestShellContextList(DecoyTestCase):
    async def test_no_arg_lists_cwd_children(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        child = make_node(id="child-id", name="file.txt")
        self.decoy.when(await drive.get_children(home)).then_return([child])
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("ls")
        # then
        mock_cout.assert_called_once_with("file.txt")

    async def test_with_path_lists_target_children(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        child = make_node(id="child-id", name="doc.txt")
        self.decoy.when(await drive.get_node_by_path(PurePath("/docs"))).then_return(
            target
        )
        self.decoy.when(await drive.get_children(target)).then_return([child])
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("ls /docs")
        # then
        mock_cout.assert_called_once_with("doc.txt")


class TestShellContextPwd(DecoyTestCase):
    async def test_prints_resolved_path(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("pwd")
        # then
        mock_cout.assert_called_once_with(PurePath("/home"))


class TestShellContextMkdir(DecoyTestCase):
    async def test_creates_directory(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_return(
            home
        )
        ctx = ShellContext(drive, home)
        # when
        await ctx.execute("mkdir newdir")
        # then
        self.decoy.verify(await drive.create_directory("newdir", home), times=1)

    async def test_relative_path_creates_under_resolved_parent(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        parent = make_node(id="parent-id", name="subdir", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_return(
            parent
        )
        ctx = ShellContext(drive, home)
        # when
        await ctx.execute("mkdir subdir/newdir")
        # then
        self.decoy.verify(await drive.create_directory("newdir", parent), times=1)

    async def test_nonexistent_parent_prints_error(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("mkdir subdir/newdir")
        # then
        mock_cout.assert_called_once()


class TestShellContextSync(DecoyTestCase):
    async def test_prints_each_change(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)

        async def fake_sync() -> AsyncIterator[ChangeAction]:
            yield (True, "change1")
            yield (True, "change2")

        self.decoy.when(drive.sync()).then_return(fake_sync())
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("sync")
        # then
        self.assertEqual(mock_cout.call_count, 2)


class TestShellContextFind(DecoyTestCase):
    async def test_prints_id_and_path_for_each_result(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        node1 = make_node(id="id1", name="file1.txt")
        node2 = make_node(id="id2", name="file2.txt")
        bind_find_nodes(drive, decoy=self.decoy)
        self.decoy.when(
            await drive.query_nodes(NodeQuery(name_pattern="pattern", is_trashed=None))
        ).then_return([node1, node2])
        self.decoy.when(await drive.resolve_path(node1)).then_return(
            PurePath("/a/file1.txt")
        )
        self.decoy.when(await drive.resolve_path(node2)).then_return(
            PurePath("/a/file2.txt")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("find pattern")
        # then
        printed = [call.args[0] for call in mock_cout.call_args_list]
        self.assertIn("id1 - /a/file1.txt", printed)
        self.assertIn("id2 - /a/file2.txt", printed)


class TestShellContextInfo(DecoyTestCase):
    async def test_with_id_prints_yaml(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_id("file-id")).then_return(target)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.print_as_yaml") as mock_yaml:
            await ctx.execute("info file-id")
        # then
        mock_yaml.assert_called_once()

    async def test_with_path_prints_yaml(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_id(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/file.txt"))
        ).then_return(target)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.print_as_yaml") as mock_yaml:
            await ctx.execute("info /file.txt")
        # then
        mock_yaml.assert_called_once()

    async def test_not_found_prints_null(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_id(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("info /no/such")
        # then
        mock_cout.assert_called_once_with("null")


class TestShellContextHash(DecoyTestCase):
    async def test_prints_hash_and_arg(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        target = replace(
            make_node(name="file.txt"), hash="abc123", hash_algorithm="md5"
        )
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_id(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/file.txt"))
        ).then_return(target)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("hash /file.txt")
        # then
        mock_cout.assert_called_once_with("abc123 - /file.txt")


class TestShellContextIdToPath(DecoyTestCase):
    async def test_prints_path_for_id(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.get_node_by_id("file-id")).then_return(target)
        self.decoy.when(await drive.resolve_path(target)).then_return(
            PurePath("/path/to/file.txt")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("id_to_path file-id")
        # then
        mock_cout.assert_called_once_with(PurePath("/path/to/file.txt"))

    async def test_not_found_prints_message(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        self.decoy.when(await drive.get_node_by_id("missing-id")).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("id_to_path missing-id")
        # then
        mock_cout.assert_called_once_with("missing-id not found")


class TestShellContextPathToId(DecoyTestCase):
    async def test_prints_id_for_path(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/file.txt"))
        ).then_return(target)
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("path_to_id /file.txt")
        # then
        mock_cout.assert_called_once_with("file-id")

    async def test_not_found_prints_message(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(is_directory=True)
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        with patch("wcpan.drive.client.cli._interaction.cout") as mock_cout:
            await ctx.execute("path_to_id /no/such")
        # then
        mock_cout.assert_called_once_with("/no/such not found")


class TestNormalizePath(DecoyTestCase):
    async def test_absolute_path_returned_as_is(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        # when
        result = await normalize_path(drive, home, "/absolute/path")
        # then
        self.assertEqual(result, PurePath("/absolute/path"))
        self.decoy.verify(await drive.resolve_path(matchers.Anything()), times=0)

    async def test_relative_path_resolved_against_cwd(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        # when
        result = await normalize_path(drive, home, "relative")
        # then
        self.assertEqual(result, PurePath("/home/relative"))


class TestGetNodeByPathOrId(DecoyTestCase):
    async def test_finds_by_id(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.get_node_by_id("file-id")).then_return(target)
        # when
        result = await get_node_by_path_or_id(drive, PurePath("/home"), "file-id")
        # then
        self.assertIs(result, target)
        self.decoy.verify(await drive.get_node_by_path(matchers.Anything()), times=0)

    async def test_falls_through_to_absolute_path(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.get_node_by_id(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/absolute/path"))
        ).then_return(target)
        # when
        result = await get_node_by_path_or_id(
            drive, PurePath("/home"), "/absolute/path"
        )
        # then
        self.assertIs(result, target)

    async def test_falls_through_to_relative_path(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        target = make_node(id="file-id", name="file.txt")
        self.decoy.when(await drive.get_node_by_id(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/home/relative"))
        ).then_return(target)
        # when
        result = await get_node_by_path_or_id(drive, PurePath("/home"), "relative")
        # then
        self.assertIs(result, target)


class TestShellContextIterPathCompletions(DecoyTestCase):
    async def test_existing_path_yields_children(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        target = make_node(id="target-id", name="docs", is_directory=True)
        child1 = make_node(id="c1", name="file1.txt")
        child2 = make_node(id="c2", name="file2.txt")
        self.decoy.when(await drive.get_node_by_path(PurePath("/docs"))).then_return(
            target
        )
        self.decoy.when(await drive.get_children(target)).then_return([child1, child2])
        ctx = ShellContext(drive, home)
        # when
        names = [name async for name in ctx.iter_path_completions("/docs")]
        # then
        self.assertIn("file1.txt", names)
        self.assertIn("file2.txt", names)

    async def test_incomplete_path_falls_back_to_parent(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        parent = make_node(id="parent-id", name="docs", is_directory=True)
        child = make_node(id="c1", name="file1.txt")
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(
            await drive.get_node_by_path(PurePath("/home/partial"))
        ).then_raise(NodeNotFoundError("not found"))
        self.decoy.when(await drive.get_node_by_path(PurePath("/home"))).then_return(
            parent
        )
        self.decoy.when(await drive.get_children(parent)).then_return([child])
        ctx = ShellContext(drive, home)
        # when
        names = [name async for name in ctx.iter_path_completions("partial")]
        # then
        self.assertIn("file1.txt", names)

    async def test_completely_missing_path_yields_nothing(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        self.decoy.when(await drive.resolve_path(home)).then_return(PurePath("/home"))
        self.decoy.when(await drive.get_node_by_path(matchers.Anything())).then_raise(
            NodeNotFoundError("not found")
        )
        ctx = ShellContext(drive, home)
        # when
        names = [name async for name in ctx.iter_path_completions("missing")]
        # then
        self.assertEqual(names, [])


class TestDriveCompleter(DecoyTestCase):
    async def test_partial_command_yields_matching_completions(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)
        doc = Document("l", 1)
        # when
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        # then
        names = [c.text for c in completions]
        self.assertIn("ls", names)
        for name in names:
            self.assertTrue(name.startswith("l"))

    async def test_nonmatching_prefix_yields_nothing(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)
        doc = Document("zzz", 3)
        # when
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        # then
        self.assertEqual(completions, [])

    async def test_path_argument_yields_path_completions(self):
        # given
        drive = self.decoy.mock(cls=Drive)
        home = make_node(name="home", is_directory=True)
        ctx = ShellContext(drive, home)
        completer = DriveCompleter(ctx)

        async def fake_iter(token: str):
            yield "file1.txt"
            yield "file2.txt"

        ctx.iter_path_completions = fake_iter
        doc = Document("ls /file", 8)
        # when
        completions = [
            c async for c in completer.get_completions_async(doc, CompleteEvent())
        ]
        # then
        names = [c.text for c in completions]
        self.assertIn("file1.txt", names)
        self.assertIn("file2.txt", names)

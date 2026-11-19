"""Maintenance commands are parsed from the factory before anything starts."""

import argparse
import io
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from typing import Annotated, Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

from wcpan.drive.plugin import check_settings
from wcpan.drive.server._backend import (
    Access,
    BackendCommand,
    CoreServices,
    ReadCoreServices,
    backend_commands,
    load_command_parser,
    writing,
)
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.model import (
    BackendNamespace,
)
from wcpan.drive.server._services.mirror.store import (
    MirrorReader,
    MirrorStore,
)
from wcpan.drive.server._services.sync.service import NodeSyncService


def _parse(name: str, argv: list[str]) -> BackendCommand:
    return BackendCommand(access=Access.READ, args=argparse.Namespace(argv=argv))


@backend_commands(_parse)
@asynccontextmanager
async def _factory(
    context: object, *, depth: Annotated[int, "Depth."] = 1
) -> AsyncGenerator[Any, None]:
    yield None


@asynccontextmanager
async def _bare(context: object) -> AsyncGenerator[Any, None]:
    yield None


class TestBackendCommand(TestCase):
    def test_a_read_command_cannot_probe(self):
        # when / then
        with self.assertRaisesRegex(ValueError, "cannot probe"):
            BackendCommand(access=Access.READ, probes=True, args=argparse.Namespace())

    def test_a_write_command_may_probe(self):
        # when
        command = BackendCommand(
            access=Access.WRITE, probes=True, args=argparse.Namespace()
        )
        # then
        self.assertTrue(command.probes)


class TestCommandParser(TestCase):
    def test_parser_is_read_from_the_factory(self):
        # when
        command = load_command_parser(_factory)("x", ["a"])
        # then
        self.assertEqual(command.args.argv, ["a"])

    def test_decorator_keeps_the_settings_signature(self):
        # when / then
        self.assertEqual(check_settings(_factory, {"depth": 2}), {"depth": 2})
        with self.assertRaisesRegex(ValueError, "unknown settings"):
            check_settings(_factory, {"other": 1})

    def test_a_factory_without_commands_rejects_every_command(self):
        # given
        parse = load_command_parser(_bare)
        for argv in ([], ["x"], ["--help"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                # when / then
                parse("x", argv)


def _read_core(schemas) -> ReadCoreServices:
    # The real store satisfies the read-only protocol (checked by pyright).
    mirror: MirrorReader = MagicMock(spec=MirrorStore)
    return ReadCoreServices(
        mirror=mirror,
        ids=MagicMock(),
        media=MediaProbing(runner=None),
        schemas=schemas,
    )


def _conforms(mirror: MirrorStore) -> None:
    """Never called; pyright checks the assignment."""
    _mirror: MirrorReader = mirror


class TestCoreAccess(IsolatedAsyncioTestCase):
    async def test_read_core_binds_through_its_checker(self):
        # given
        ns = BackendNamespace(1)
        schemas = AsyncMock(return_value=ns)
        core = _read_core(schemas)
        schema = MagicMock()
        # when
        bound = await core.bind_schema(schema)
        # then
        self.assertIs(bound, ns)
        schemas.assert_awaited_once_with(schema)

    def test_writing_refuses_a_read_core(self):
        # when / then
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            writing(_read_core(AsyncMock()))

    def test_writing_passes_a_write_core(self):
        # given
        core = CoreServices(
            mirror=MagicMock(),
            node_sync=MagicMock(),
            ids=MagicMock(),
            media=MediaProbing(runner=None),
        )
        # when / then
        self.assertIs(writing(core), core)

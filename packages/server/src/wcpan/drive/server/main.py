"""``wcpan.drive.server`` command.

The config names the backend factories to load; their packages only need to be
installed, for example::

    uvx --with=wcpan-drive-server-synology wcpan.drive.server --config server.yaml serve

Backend maintenance commands are addressed to one instance and parsed by it::

    wcpan.drive.server --config server.yaml backend nas backfill /photos
    wcpan.drive.server --config server.yaml backend nas --help

``init`` prints a starting config for installed backend plugins, without
reading any config::

    wcpan.drive.server init --plugin synology --plugin filesystem > server.yaml
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from logging.config import dictConfig
from pathlib import Path
from typing import cast

from aiohttp import web
from wcpan.drive.plugin import factory_path, list_plugins
from wcpan.logging import ConfigBuilder

from .app import create_app, entered_backends, managed_core, managed_off_main
from .backend import BACKEND_PLUGIN_GROUP, load_factory
from .config import ConfigVersionError, load_config
from .scaffold import create_config_scaffold
from .services.mirror import (
    SchemaVersionError,
    cleanup_dangling_nodes,
    drop_backend,
    rebuild_backend,
    registered_instances,
    reset_change_history,
)
from .types import ServerConfig
from .workers import noop_after_write


_L = logging.getLogger(__name__)

_DEFAULT_CONFIG = "/data/server.yaml"


_LIFECYCLE = ("drop", "rebuild")


def _parser(
    config: ServerConfig | None, registered: Sequence[str] = ()
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wcpan.drive.server")
    parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"Path to YAML config file (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the mirror server")
    subparsers.add_parser(
        "gc",
        help="Remove nodes unreachable from the server root or left under a mount"
        " by a backend that no longer serves it, and prune unused node IDs",
    )
    subparsers.add_parser(
        "squash", help="[DANGER] Reset change history to a single update per node"
    )
    init = subparsers.add_parser(
        "init",
        help="Print a starting config for installed backend plugins",
        description="Required settings are left empty to fill in; "
        "the config file is not read.",
    )
    init.add_argument(
        "--plugin",
        "-p",
        action="append",
        default=[],
        help="Backend plugin by name, module, or factory path; repeatable",
    )
    init.add_argument(
        "--list", action="store_true", help="List installed backend plugins"
    )
    instances = {name: b.type for name, b in config.backends.items()} if config else {}
    unconfigured = [n for n in registered if n not in instances]
    backend = subparsers.add_parser(
        "backend",
        help="Run a maintenance command of one backend instance",
        description="Arguments after NAME are parsed by that backend; "
        "see `backend NAME --help`. The server itself handles "
        "`backend NAME drop` (remove an instance no longer configured, with its "
        "nodes) and `backend NAME rebuild` (remove a configured instance's "
        "nodes and tables, so the next start discovers them again).",
        epilog="configured instances: "
        + (", ".join(f"{n} ({t})" for n, t in instances.items()) or "none")
        + "".join(f"; {n} (not configured)" for n in unconfigured),
    )
    backend.add_argument(
        "name",
        choices=[*instances, *unconfigured] or None,
        help="Backend instance name",
    )
    backend.add_argument("args", nargs=argparse.REMAINDER, help="Backend command")
    return parser


def _single(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The only error inside nested task groups, or the group itself."""
    if len(group.exceptions) != 1:
        return group
    inner = group.exceptions[0]
    if isinstance(inner, BaseExceptionGroup):
        return _single(cast(BaseExceptionGroup[BaseException], inner))
    return inner


def _fail(message: object) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


async def run_backend_command(
    config: ServerConfig, name: str, argv: Sequence[str]
) -> None:
    """Enter instance *name* offline and run its command; the server must be stopped."""
    async with AsyncExitStack() as stack:
        off_main = await stack.enter_async_context(managed_off_main())
        core = await stack.enter_async_context(
            managed_core(config, off_main=off_main, after_write=noop_after_write)
        )
        backends = await stack.enter_async_context(
            entered_backends(config, core, offline=True, names=[name])
        )
        await backends[name].command(argv)
        await core.node_sync.wait_enrichment_drained()


def _lifecycle_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"wcpan.drive.server backend {name}")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser(
        "drop",
        help="[DANGER] Remove this instance, which is no longer configured, with "
        "its nodes, node IDs, and tables",
    )
    subparsers.add_parser(
        "rebuild",
        help="[DANGER] Remove this instance's nodes and tables; the next start "
        "discovers them again",
    )
    return parser


def run_lifecycle_command(config: ServerConfig, name: str, argv: Sequence[str]) -> None:
    """Drop or rebuild instance *name*; the server must be stopped.

    Raises:
        ValueError: For a configured instance to drop, or an unconfigured one
            to rebuild.
    """
    action = _lifecycle_parser(name).parse_args(list(argv)).action
    configured = name in config.backends
    if action == "drop":
        if configured:
            raise ValueError(
                f"backend {name!r} is configured; remove it from the config "
                "first, or rebuild it"
            )
        count = drop_backend(config.database_url, name)
        print(f"Dropped backend {name!r}: {count} node(s) removed.")
        return
    if not configured:
        raise ValueError(f"backend {name!r} is not configured; drop it instead")
    count = rebuild_backend(config.database_url, name)
    print(
        f"Backend {name!r} will be rediscovered on the next start:"
        f" {count} node(s) removed."
    )


def _init(args: argparse.Namespace) -> None:
    if args.list:
        for ep in list_plugins([BACKEND_PLUGIN_GROUP]):
            print(f"{ep.name}\t{factory_path(ep)}")
        return
    try:
        print(create_config_scaffold(args.plugin), end="")
    except ValueError as e:
        _fail(e)


def main(argv: Sequence[str] | None = None) -> None:
    # Find the config first, without handling --help, so help can list the
    # configured backend instances.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=_DEFAULT_CONFIG)
    pre.add_argument("--log-level")
    pre.add_argument("command", nargs="?")
    known, _ = pre.parse_known_args(argv)
    if known.command == "init":
        _init(_parser(None).parse_args(argv))
        return
    config_path = Path(known.config)
    if not config_path.exists():
        # Still parse, so --help works without a config.
        _parser(None).parse_args(argv)
        _fail(f"Config file not found: {config_path}")
        return

    try:
        config = load_config(config_path)
        # Fail on a missing backend package before doing anything else.
        for backend in config.backends.values():
            load_factory(backend.type)
    except (ConfigVersionError, ValueError) as e:
        _fail(e)
        return
    args = _parser(config, registered_instances(config.database_url)).parse_args(argv)

    try:
        if args.command == "gc":
            count = cleanup_dangling_nodes(
                config.database_url,
                mounts={name: m.backend for name, m in config.mounts.items()},
            )
            print(f"Removed {count} dangling node(s).")
            return

        if args.command == "backend" and args.args[:1] and args.args[0] in _LIFECYCLE:
            run_lifecycle_command(config, args.name, args.args)
            return

        if args.command == "backend" and args.name not in config.backends:
            _fail(
                f"backend {args.name!r} is not configured; only "
                f"`backend {args.name} drop` applies"
            )
            return

        if args.command == "squash":
            print("WARNING: squash resets all consumer cursors.", file=sys.stderr)
            count = reset_change_history(config.database_url)
            print(f"Reset change history: {count} update record(s) written.")
            return

        dictConfig(
            ConfigBuilder(path=config.log_path)
            .add("wcpan.drive.server", level=args.log_level)
            .add("aiohttp")
            .to_dict()
        )

        try:
            if args.command == "backend":
                asyncio.run(run_backend_command(config, args.name, args.args))
                return

            app = create_app(config)
            _L.info("listening on %s:%s", config.host, config.port)
            web.run_app(app, host=config.host, port=config.port, print=None)
        except BaseExceptionGroup as group:
            raise _single(group) from None
    except (ConfigVersionError, SchemaVersionError, ValueError, OSError) as e:
        _fail(e)


if __name__ == "__main__":
    main()

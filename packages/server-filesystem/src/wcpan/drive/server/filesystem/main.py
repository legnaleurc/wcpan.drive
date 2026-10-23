"""CLI entry point for a server backed by local directories."""

import argparse
import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from logging.config import dictConfig
from pathlib import Path

from aiohttp import web
from wcpan.drive.server.app import create_app
from wcpan.drive.server.config import ConfigVersionError
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.storage import (
    SchemaVersionError,
    cleanup_dangling_nodes,
    create_storage_service,
    reset_change_history,
)
from wcpan.drive.server.types import ServerConfig
from wcpan.logging import ConfigBuilder

from .backend import FilesystemBackend
from .config import load_config
from .handles import filesystem_id
from .store import FILESYSTEM_SCHEMA, FilesystemStore


_L = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="wcpan.drive.server.filesystem")
    parser.add_argument(
        "--config",
        default="/data/server.yaml",
        help="Path to YAML config file (default: /data/server.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the read-only filesystem mirror server")
    subparsers.add_parser(
        "gc", help="Remove dangling nodes unreachable from the server root"
    )
    subparsers.add_parser(
        "squash", help="[DANGER] Reset change history to a single update per node"
    )
    rebind_p = subparsers.add_parser(
        "rebind-mount",
        help="Accept that a mount's source now lives on a different filesystem",
    )
    rebind_p.add_argument("name", help="Mount name from the config")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config, filesystem = load_config(config_path)

        if args.command == "gc":
            count = cleanup_dangling_nodes(
                config.database_url, backend=FILESYSTEM_SCHEMA
            )
            print(f"Removed {count} dangling node(s).")
            return

        if args.command == "squash":
            print("WARNING: squash resets all consumer cursors.", file=sys.stderr)
            count = reset_change_history(config.database_url, backend=FILESYSTEM_SCHEMA)
            print(f"Reset change history: {count} update record(s) written.")
            return

        if args.command == "rebind-mount":
            fsid = asyncio.run(rebind_mount(config, args.name))
            print(f"Mount {args.name!r} now expects filesystem {fsid}.")
            return

        dictConfig(
            ConfigBuilder(path=config.log_path)
            .add("wcpan.drive.server", level=args.log_level)
            .add("aiohttp")
            .to_dict()
        )
        app = create_app(
            config,
            backend=FilesystemBackend(config=filesystem, mounts=config.mounts),
        )
        _L.info("listening on %s:%s", config.host, config.port)
        web.run_app(app, host=config.host, port=config.port, print=None)
    except (ConfigVersionError, SchemaVersionError, ValueError, OSError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


async def rebind_mount(config: ServerConfig, name: str) -> str:
    """Record the filesystem a mount's source is on now; run with the server stopped."""
    source = config.mounts.get(name)
    if source is None:
        raise ValueError(f"Unknown mount {name!r}")
    with ThreadPoolExecutor(1) as pool:
        storage = await create_storage_service(
            config.database_url,
            off_main=OffMainService(pool=pool),
            backend=FILESYSTEM_SCHEMA,
        )
        fsid = filesystem_id(source)
        await FilesystemStore(storage=storage).set_mount_fsid(name, source, fsid)
    return fsid


if __name__ == "__main__":
    main()

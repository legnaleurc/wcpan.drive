"""CLI entry point for the wcpan.drive.server server."""

import argparse
import asyncio
import logging
import sys
from contextlib import AsyncExitStack, suppress
from logging.config import dictConfig
from pathlib import Path

from aiohttp import web
from wcpan.drive.server.synology import create_synology_client
from wcpan.logging import ConfigBuilder

from .app import create_app, managed_off_main
from .config import ConfigVersionError, load_config
from .lib.mounts import create_mount_registry
from .services.backfill import BackfillService
from .services.paths import SynologyPathService
from .services.storage import (
    SchemaVersionError,
    cleanup_dangling_nodes,
    create_storage_service,
    reset_change_history,
)
from .services.sync import NodeSyncService
from .types import ServerConfig
from .workers import (
    METADATA_WORKER_COUNT,
    create_metadata_queue,
    create_write_queue,
    metadata_worker,
    noop_after_write,
    write_worker,
)


_L = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="wcpan.drive.server")
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
    subparsers.add_parser("serve", help="Run the Synology mirror server")
    subparsers.add_parser(
        "gc", help="Remove dangling nodes unreachable from the server root"
    )
    backfill_p = subparsers.add_parser(
        "backfill",
        help="Reconcile node metadata under PATH with Synology Drive API",
    )
    backfill_p.add_argument(
        "path",
        type=str,
        help="Virtual directory path in the mirror (e.g. / for root, /photos/Projects)",
    )
    backfill_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing the database",
    )
    subparsers.add_parser(
        "squash", help="[DANGER] Reset change history to a single update per node"
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config(config_path)

        if args.command == "gc":
            count = cleanup_dangling_nodes(config.database_url)
            print(f"Removed {count} dangling node(s).")
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

        if args.command == "backfill":
            try:
                stats = asyncio.run(
                    run_backfill(
                        config,
                        args.path.strip(),
                        dry_run=args.dry_run,
                    )
                )
            except ValueError as e:
                print(e, file=sys.stderr)
                sys.exit(1)
            print(
                f"Checked {stats['checked']} node(s), "
                f"added {stats['added']}, "
                f"updated {stats['updated']}, "
                f"removed {stats['removed']}, "
                f"list errors {stats['list_errors']}."
            )
            return

        app = create_app(config)
        _L.info("listening on %s:%s", config.host, config.port)
        web.run_app(app, host=config.host, port=config.port, print=None)
    except (ConfigVersionError, SchemaVersionError, ValueError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


async def run_backfill(
    config: ServerConfig,
    virtual_path: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Build ``BackfillService`` with the same stack as server startup (no HTTP/webhook)."""
    async with AsyncExitStack() as stack:
        drive_api = await stack.enter_async_context(create_synology_client(config))
        off_main = await stack.enter_async_context(managed_off_main())
        storage = await create_storage_service(config.database_url, off_main=off_main)
        write_queue = create_write_queue()
        metadata_queue = create_metadata_queue()
        node_sync = NodeSyncService(
            storage=storage,
            write_queue=write_queue,
            off_main=off_main,
            mounts=config.mounts,
            local_paths=config.local_paths,
            metadata_queue=metadata_queue,
        )
        mount_registry = await create_mount_registry(
            config.mounts,
            drive_api=drive_api,
        )
        syno_paths = SynologyPathService(
            registry=mount_registry,
            storage=storage,
        )
        backfill = BackfillService(
            drive_api=drive_api,
            storage=storage,
            syno_paths=syno_paths,
            node_sync=node_sync,
        )
        write_task = asyncio.create_task(write_worker(write_queue, noop_after_write))
        meta_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(
                metadata_worker(metadata_queue, node_sync.process_metadata_item)
            )
            for _ in range(METADATA_WORKER_COUNT)
        ]
        try:
            stats = await backfill.run_virtual_path(virtual_path, dry_run=dry_run)
            await metadata_queue.join()
            await write_queue.join()
            return stats
        finally:
            for t in meta_tasks:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t
            write_task.cancel()
            with suppress(asyncio.CancelledError):
                await write_task


if __name__ == "__main__":
    main()

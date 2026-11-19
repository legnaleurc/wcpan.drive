"""Helpers for Synology tests that reach into the core."""

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server._services.sync.scan import compare_pass
from wcpan.drive.server._services.sync.scanner import ComparisonReport
from wcpan.drive.server._services.sync.writer import MirrorWriter
from wcpan.drive.server.backend.synology._backfill import SubtreeComparison
from wcpan.drive.server.backend.synology._paths import SynologyPathService
from wcpan.drive.server.backend.synology._store import SynologyStoreReader
from wcpan.drive.server.types import MirrorReader
from wcpan.synology.types import SynologyClient


async def backfill(
    root_id: NodeId,
    *,
    drive_api: SynologyClient,
    mirror: MirrorReader,
    store: SynologyStoreReader,
    syno_paths: SynologyPathService,
    writer: MirrorWriter | None,
) -> ComparisonReport:
    """Run the ``backfill`` comparison of *root_id*, as the command would.

    Without a *writer* it is a dry run.
    """
    comparison = SubtreeComparison(
        root_id,
        drive_api=drive_api,
        mirror=mirror,
        store=store,
        syno_paths=syno_paths,
    )
    return await compare_pass(comparison, mirror=mirror, writer=writer)

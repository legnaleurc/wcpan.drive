"""Helpers for Synology tests that reach into the core."""

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server._services.sync.ordering import NodeOrdering
from wcpan.drive.server._services.sync.scan import ComparisonReport, compare_pass
from wcpan.drive.server._services.sync.service import NodeSyncService
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
    node_sync: NodeSyncService | NodeOrdering,
    dry_run: bool = False,
) -> ComparisonReport:
    """Run the ``backfill`` comparison of *root_id*, as the command would."""
    comparison = SubtreeComparison(
        root_id,
        drive_api=drive_api,
        mirror=mirror,
        store=store,
        syno_paths=syno_paths,
    )
    return await compare_pass(
        comparison, mirror=mirror, node_sync=node_sync, dry_run=dry_run
    )

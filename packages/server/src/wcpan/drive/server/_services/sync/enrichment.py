"""What a backend controls of media enrichment.

See "Backend framework" in the package ``SPEC.md``.
"""

from contextlib import AbstractAsyncContextManager

from ..media.retries import RetryMedia
from .service import LocateSource, NodeSyncService


class Enrichment:
    """Waiting for enrichment, and retrying this backend's failed probes."""

    def __init__(self, node_sync: NodeSyncService) -> None:
        self._node_sync = node_sync

    async def drain(self) -> None:
        """Wait for queued enrichment and the writes it produces, not retries."""
        await self._node_sync.wait_enrichment_drained()

    def scope(self) -> AbstractAsyncContextManager[None]:
        """Run enrichment workers while entered, for offline commands."""
        return self._node_sync.enrichment_scope()

    async def retry_failures(self, locate: LocateSource) -> None:
        """Observe every recorded failure again with *locate*, and probe it."""
        await self._node_sync.retry_media_failures(locate=locate)

    def retrying(self, locate: LocateSource) -> AbstractAsyncContextManager[None]:
        """Retry transient failures with backoff while entered, through *locate*."""
        return self._node_sync.retrying(locate)

    def retrying_with(self, retry: RetryMedia) -> AbstractAsyncContextManager[None]:
        """Like ``retrying``, for a backend that observes files its own way."""
        return self._node_sync.retrying_with(retry)

    async def prune_failures(self, *, before: int) -> int:
        """Drop failure records last marked before *before* (ms)."""
        return await self._node_sync.prune_media_failures(before=before)

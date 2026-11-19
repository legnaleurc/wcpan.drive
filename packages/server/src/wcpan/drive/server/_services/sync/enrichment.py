"""What a backend controls of media enrichment: waiting, and retrying failed probes.

The retries are a producer like any other: each attempt re-observes a file under
a fresh read and submits it for probing again. See "Backend framework" in the
package ``SPEC.md``.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from functools import partial
from logging import getLogger

from ..._types import NodeObservation
from ..media.retries import RetryMedia
from ..mirror.model import now_ms
from ..operations import LocateUndecidedError
from .writer import MirrorWriter


_L = getLogger(__name__)

type LocateSource = Callable[[str], Awaitable[NodeObservation | None]]
"""Observe the item with a source key now; ``None`` if the backend no longer has
it under a mirrored parent. Raises ``LocateUndecidedError`` when that cannot be
decided."""


class Enrichment:
    """Waiting for enrichment, and retrying this backend's failed probes."""

    def __init__(self, writer: MirrorWriter) -> None:
        """*writer* is the backend's view."""
        self._writer = writer

    async def drain(self) -> None:
        """Wait for queued enrichment and the writes it produces, not retries."""
        await self._writer.wait_enrichment_drained()

    def scope(self) -> AbstractAsyncContextManager[None]:
        """Run enrichment workers while entered, for offline commands."""
        return self._writer.enrichment_scope()

    async def retry_failures(self, locate: LocateSource) -> None:
        """Observe every recorded failure again with *locate*, and probe it.

        Failures a backoff retry already covers are skipped, whatever their
        kind. A record whose file the backend no longer has is dropped.
        Used by: backends whose discovery would not see an unchanged file
        again, once at startup.
        """
        pending = self._writer.retries.pending(self._writer.backend)
        for failure in await self._writer.media_failures():
            if failure.source_key in pending:
                continue
            try:
                await self._reprobe(failure.source_key, locate=locate)
            except LocateUndecidedError:
                _L.warning(
                    "Could not re-observe %s; retrying later", failure.source_key
                )

    def retrying(self, locate: LocateSource) -> AbstractAsyncContextManager[None]:
        """Retry transient failures with backoff while entered, through *locate*.

        Each attempt re-observes the file with *locate* under a fresh read.
        """
        return self.retrying_with(partial(self._reprobe, locate=locate))

    def retrying_with(self, retry: RetryMedia) -> AbstractAsyncContextManager[None]:
        """Like ``retrying``, for a backend that observes files its own way."""
        return self._writer.retries.register(self._writer.backend, retry)

    async def prune_failures(self, *, before: int) -> int:
        """Drop failure records last marked before *before* (ms)."""
        return await self._writer.prune_media_failures(before=before)

    async def _reprobe(self, source_key: str, *, locate: LocateSource) -> None:
        """Observe the file with *source_key* again and submit it for probing.

        Raises:
            LocateUndecidedError: When the backend cannot tell now.
        """
        writer = self._writer
        async with writer.observe() as read:
            asked = now_ms()
            current = await locate(source_key)
            if current is None:
                await writer.drop_media_failure(source_key, found_gone=asked)
                return
            await writer.submit_probe(current, read=read)

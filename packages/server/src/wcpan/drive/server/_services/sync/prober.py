"""The media prober: an observation with its dimensions, or the failed probe of it.

It reads the mirror only to reuse stored dimensions instead of probing. What a
failed probe publishes is decided by the write that applies it.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from wcpan.drive.protocol.types import NodeRecord

from ..._types import NodeObservation, ProbeFailure
from ..media.enricher import MediaEnrichmentError, MediaEnrichService
from ..media.probing import MediaProbing
from ..media.retries import MediaRetries
from ..mirror.store import MirrorReader
from .metadata import (
    METADATA_WORKER_COUNT,
    MetadataQueue,
    MetadataWorkItem,
    metadata_worker,
)


def has_complete_media_dims(record: NodeRecord) -> bool:
    """True when *record* already has full media dimensions (no probing needed)."""
    if not (record.is_image or record.is_video):
        return True
    return record.width > 0 and record.height > 0


def same_content(a: NodeRecord, b: NodeRecord) -> bool:
    """True when *a* and *b* describe the same file content."""
    return a.size == b.size and a.hash == b.hash


def with_dims_of(record: NodeRecord, source: NodeRecord) -> NodeRecord:
    return replace(
        record,
        width=source.width,
        height=source.height,
    )


def is_media(record: NodeRecord) -> bool:
    """Whether *record* is an image or video, the only files probed."""
    return record.is_image or record.is_video


def needs_probe(existing: NodeRecord | None, record: NodeRecord) -> bool:
    """Whether *record* must be probed: media whose stored dimensions do not apply."""
    return is_media(record) and not reusable_dims(existing, record)


def reusable_dims(existing: NodeRecord | None, record: NodeRecord) -> bool:
    """True when *existing*'s media dimensions still describe *record*.

    The content must be the same, and the dimensions complete for *record*'s
    media kind, which a rename can change.
    """
    return (
        existing is not None
        and same_content(existing, record)
        and has_complete_media_dims(with_dims_of(record, existing))
    )


@dataclass(frozen=True, slots=True)
class ProbeFailed:
    """A media probe that failed; its write decides whether to keep or withhold."""

    observation: NodeObservation
    kind: ProbeFailure
    reason: str


# What a write publishes: an observation, or a failed probe of one.
type Outcome = NodeObservation | ProbeFailed


def observation_of(outcome: Outcome) -> NodeObservation:
    return outcome.observation if isinstance(outcome, ProbeFailed) else outcome


class Prober:
    """Probes media observations, inline or from the metadata workers it runs."""

    def __init__(
        self,
        *,
        mirror: MirrorReader,
        probing: MediaProbing,
        retries: MediaRetries,
        metadata_queue: MetadataQueue,
        require_workers: bool,
    ) -> None:
        """With *require_workers* (offline), queuing a probe fails unless
        ``scope`` runs the workers, since nothing else would."""
        self._mirror = mirror
        self._enricher = MediaEnrichService(probing=probing)
        self._retries = retries
        self._queue = metadata_queue
        self._required = require_workers
        # Scopes running the workers now.
        self._active = 0

    async def outcome(
        self,
        observation: NodeObservation,
        *,
        force_refresh: bool,
        strict: bool = False,
    ) -> Outcome:
        """*observation* with its dimensions, or the failed probe of it.

        Only media whose stored dimensions do not apply is probed. What a failed
        probe publishes is decided by the write that applies it.

        Raises:
            MediaEnrichmentError: With *strict*, if the probe fails.
        """
        record = observation.record
        if not is_media(record):
            # Directories and other files are never probed.
            return observation
        existing = await self._mirror.get_node_by_id(record.id)
        if existing is not None and reusable_dims(existing, record):
            return replace(observation, record=with_dims_of(record, existing))
        if observation.backend is not None and observation.source_key is not None:
            self._retries.attempting(observation.backend, observation.source_key)
        try:
            enriched = await self._enricher.enrich(
                observation, force_refresh=force_refresh
            )
        except MediaEnrichmentError as e:
            if strict:
                raise
            if observation.backend is None:
                # Only backend views observe files; the core's own nodes are
                # directories and never need enrichment.
                raise ValueError(
                    f"Untagged observation of {record.id} needs enrichment"
                ) from e
            return ProbeFailed(observation=observation, kind=e.kind, reason=e.reason)
        return replace(observation, record=enriched)

    @asynccontextmanager
    async def scope(
        self,
        process_item: Callable[[MetadataWorkItem], Awaitable[None]],
        *,
        drain: Callable[[], Awaitable[None]],
    ) -> AsyncGenerator[None, None]:
        """Run the metadata workers on *process_item* while entered.

        On a normal exit, queued enrichment and the writes it produces finish first.
        On an error, running probes are cancelled and the error is re-raised;
        queued enrichment and writes are abandoned, for the command to redo
        when run again.
        """
        queue = self._queue
        failed: BaseException | None = None
        self._active += 1
        try:
            async with asyncio.TaskGroup() as group:
                workers = [
                    group.create_task(metadata_worker(queue, process_item))
                    for _ in range(METADATA_WORKER_COUNT)
                ]
                try:
                    yield
                    await drain()
                except BaseException as e:
                    failed = e
                    raise
                finally:
                    for worker in workers:
                        worker.cancel()
        except BaseExceptionGroup as group:
            # The caller's own error alone comes out as itself, not in a group.
            if failed is not None and list(group.exceptions) == [failed]:
                raise failed from failed.__cause__
            raise
        finally:
            self._active -= 1

    def check_workers(self) -> None:
        """Fail if a probe is queued that no worker would run."""
        if self._required and not self._active:
            raise RuntimeError(
                "enrichment was queued with no metadata workers; a command that"
                " queues enrichment must declare probes and run enrichment_scope"
            )

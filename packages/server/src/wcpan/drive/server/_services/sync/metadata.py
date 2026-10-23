"""The metadata queue: async media enrichment ahead of the write queue."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger

from ..._types import NodeObservation, ReadSeq
from ..media.probe.source import ProbeUnavailableError


_L = getLogger(__name__)

METADATA_WORKER_COUNT = max(2, os.process_cpu_count() or 1)
_METADATA_QUEUE_MAXSIZE = max(16, (os.process_cpu_count() or 1) * 2)


@dataclass(frozen=True, slots=True)
class MetadataWorkItem:
    """One async media-enrichment job; consumed by ``metadata_worker``."""

    observation: NodeObservation
    force_refresh: bool
    # Start of the backend read that produced the observation.
    read: ReadSeq


type MetadataQueue = asyncio.Queue[MetadataWorkItem]


def create_metadata_queue() -> MetadataQueue:
    return asyncio.Queue(maxsize=_METADATA_QUEUE_MAXSIZE)


async def metadata_worker(
    metadata_queue: MetadataQueue,
    process_item: Callable[[MetadataWorkItem], Awaitable[None]],
) -> None:
    """Single consumer: async enrichment, then enqueue sync write jobs.

    MediaInfo that cannot be run stops the server, as a write failure does.
    """
    while True:
        item = await metadata_queue.get()
        try:
            await process_item(item)
        except ProbeUnavailableError:
            raise
        except Exception:
            _L.exception("metadata item failed: %s", item.observation.record.id)
        finally:
            metadata_queue.task_done()

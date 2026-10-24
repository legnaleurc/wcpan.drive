import asyncio
import os
from collections.abc import Awaitable, Callable
from logging import getLogger

from .lib.debounce import Debouncer
from .services.storage import StorageService
from .types import MetadataQueue, MetadataWorkItem, WriteQueue


_L = getLogger(__name__)

_WAL_CHECKPOINT_DELAY = 30.0  # seconds

METADATA_WORKER_COUNT = max(2, os.process_cpu_count() or 1)


def noop_after_write() -> None:
    pass


def create_write_queue() -> WriteQueue:
    n = os.process_cpu_count() or 1
    return asyncio.Queue(maxsize=max(8, n * 2))


_METADATA_QUEUE_MAXSIZE = max(16, (os.process_cpu_count() or 1) * 2)


def create_metadata_queue() -> MetadataQueue:
    return asyncio.Queue(maxsize=_METADATA_QUEUE_MAXSIZE)


async def metadata_worker(
    metadata_queue: MetadataQueue,
    process_item: Callable[[MetadataWorkItem], Awaitable[None]],
) -> None:
    """Single consumer: async enrichment, then enqueue sync write jobs."""
    while True:
        item = await metadata_queue.get()
        try:
            await process_item(item)
        except Exception:
            _L.exception("metadata item failed: %s", item.observation.record.id)
        finally:
            metadata_queue.task_done()


class CheckpointScheduler:
    """Debounced WAL checkpoint after writes; call it from the write worker."""

    def __init__(
        self, group: asyncio.TaskGroup, storage: StorageService, *, delay: float
    ) -> None:
        self._debouncer = Debouncer(group, delay)
        self._storage = storage

    def __call__(self) -> None:
        self._debouncer.start(self._storage.checkpoint)

    def cancel(self) -> None:
        """Drop a pending checkpoint, so shutdown need not wait for its delay."""
        self._debouncer.cancel_pending()


def create_checkpoint_scheduler(
    group: asyncio.TaskGroup,
    storage: StorageService,
    *,
    delay: float = _WAL_CHECKPOINT_DELAY,
) -> CheckpointScheduler:
    return CheckpointScheduler(group, storage, delay=delay)


# Let-it-crash on write failure by design.
# Write failure in sqlite likely means the system is unstable.
# Discard any in-memory data, and let the next initialization recover the state.
async def write_worker(
    write_queue: WriteQueue,
    after_write: Callable[[], None],
) -> None:
    while True:
        task = await write_queue.get()
        try:
            await task()
            after_write()
        except Exception:
            _L.exception("write task failed")
            raise
        finally:
            write_queue.task_done()

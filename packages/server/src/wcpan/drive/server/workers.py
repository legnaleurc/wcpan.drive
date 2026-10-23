import asyncio
import os
from collections.abc import Awaitable, Callable
from logging import getLogger

from wcpan.synology import SynologyWebhookEvent

from .lib.debounce import Debouncer
from .services.storage import StorageService
from .types import MetadataQueue, MetadataWorkItem, WriteQueue


_L = getLogger(__name__)

_WAL_CHECKPOINT_DELAY = 30.0  # seconds
type WebhookQueue = asyncio.Queue[SynologyWebhookEvent]

METADATA_WORKER_COUNT = max(2, os.process_cpu_count() or 1)


def noop_after_write() -> None:
    pass


def create_write_queue() -> WriteQueue:
    n = os.process_cpu_count() or 1
    return asyncio.Queue(maxsize=max(8, n * 2))


def create_webhook_queue() -> WebhookQueue:
    return asyncio.Queue()


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
            _L.exception("metadata item failed: %s", item.record.id)
        finally:
            metadata_queue.task_done()


def create_checkpoint_scheduler(
    group: asyncio.TaskGroup,
    storage: StorageService,
    *,
    delay: float = _WAL_CHECKPOINT_DELAY,
) -> Callable[[], None]:
    debouncer = Debouncer(group, delay)

    def schedule_checkpoint() -> None:
        debouncer.start(storage.checkpoint)

    return schedule_checkpoint


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
